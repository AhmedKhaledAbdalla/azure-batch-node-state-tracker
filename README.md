# Azure Batch Node State Tracker

A tiny Python collector that closes a real gap in Azure Batch monitoring: **per-node state history and duration**.

## The problem

Azure Monitor for `Microsoft.Batch/batchAccounts` publishes counters such as `LeavingPoolNodeCount`, `UnusableNodeCount`, `StartTaskFailedNodeCount`, `RunningNodeCount`, `IdleNodeCount`, and friends -- but **none of them carry a `nodeId` (or even a `poolId`) dimension**. They are single integers for the whole Batch account.

So the platform can tell you "3 nodes are leaving the pool", but it cannot answer the questions on-call actually needs:

- **Which** node is stuck?
- In **which** pool?
- For **how long** has it been in that state?
- **Why** did it become `unusable`? (What was `errors[0].code`?)

`AzureDiagnostics` ServiceLog does not emit a per-node `NodeStateChange` event either. The older KQL samples that look for `properties.eventType == "NodeStateChange"` return zero rows on today's schema.

## The solution

The Azure Batch REST API `GET /pools/{poolId}/nodes` returns everything you need -- `state`, `stateTransitionTime`, `errors[]`, `ipAddress`, `allocationTime`, `lastBootTime`, `startTaskInfo`, and so on. This script polls it on a schedule and posts each snapshot as a row to a Log Analytics custom table `BatchNodeInventory_CL`.

With one KQL query you can then answer *"which nodes have been in state X for longer than N minutes, and why?"* -- and wire that to an Azure Monitor alert rule.

```
+---------------+    REST poll     +------------+   HTTP DCC   +-------------------+
| Azure Batch   | ---------------> |  Collector | -----------> | Log Analytics     |
| pools / nodes |  every 60 s      |  (Python)  |  one POST    | BatchNodeInventory|
+---------------+                  +------------+              +---------+---------+
                                                                         |
                                                                         v
                                                            +------------------------+
                                                            | KQL / Grafana / Alert  |
                                                            +------------------------+
```

## Quick start

Requires Python 3.9+ and the Azure CLI (`az`).

```powershell
# 1. Sign in and pick the subscription that hosts your Batch account
az login
az account set --subscription "<your-subscription>"

# 2. Get the LAW workspace primary shared key and store it in %TEMP%\.law-key
az monitor log-analytics workspace get-shared-keys `
    -g <rg> -n <workspace> --query primarySharedKey -o tsv `
    > "$env:TEMP\.law-key"

# 3. Point the collector at your workspace / account (via env vars or edit constants)
$env:LAW_WORKSPACE_ID = "<workspace-customer-id-guid>"
$env:BATCH_ACCOUNT    = "<my-batch-account>"
$env:BATCH_REGION     = "westus2"

# 4. Run once, or as a daemon
python node-state-collector.py --once
python node-state-collector.py --loop --interval 60
```

A single snapshot looks like this in `BatchNodeInventory_CL`:

| PoolId | NodeId | State | StateTransitionTime | IpAddress | ErrorCode |
|---|---|---|---|---|---|
| pool-cpu-dedicated | tvmps_4306456424a0e7e1... | rebooting | 2026-08-17T20:53:05Z | 10.60.1.4 |  |
| pool-spot | tvmps_729327cb38d16d74... | leavingpool | 2026-08-17T12:29:27Z | 10.60.1.6 |  |
| pool-autoscale | tvmps_93f33224f733877d... | starting | 2026-08-17T20:53:48Z | 10.60.1.11 |  |

## Useful KQL

**Per-node view with duration** -- the answer the platform never gives you:

```kusto
BatchNodeInventory_CL
| where TimeGenerated > ago(1h)
| summarize LastSeen=max(TimeGenerated),
            State=any(State_s), Pool=any(PoolId_s), IP=any(IPAddress),
            Transition=any(StateTransitionTime_t),
            ErrCode=any(ErrorCode_s)
      by NodeId=NodeId_s
| extend MinutesInState = datetime_diff("minute", LastSeen, Transition)
| project Pool, NodeId, State, IP, MinutesInState, ErrorCode
| order by MinutesInState desc
```

**Alert rule** -- fires on stuck / bad-state nodes:

```kusto
BatchNodeInventory_CL
| where TimeGenerated > ago(10m)
| summarize LastSeen=max(TimeGenerated), State=any(State_s), Pool=any(PoolId_s),
            IP=any(IPAddress), Transition=any(StateTransitionTime_t),
            ErrCode=any(ErrorCode_s), ErrMsg=any(ErrorMessage_s)
      by NodeId=NodeId_s
| extend MinutesInState = datetime_diff("minute", LastSeen, Transition)
| where State in ("unusable","leavingpool","starttaskfailed","offline")
     or MinutesInState > 60
```

**Per-pool live counts** -- same table, aggregated:

```kusto
BatchNodeInventory_CL
| where TimeGenerated > ago(30m)
| summarize Nodes=dcount(NodeId_s),
            Idle=dcountif(NodeId_s, State_s=="idle"),
            Running=dcountif(NodeId_s, State_s=="running"),
            Unusable=dcountif(NodeId_s, State_s=="unusable"),
            StartTaskFailed=dcountif(NodeId_s, State_s=="starttaskfailed")
      by Pool=PoolId_s
```

## Production deployment

| Option | When to pick it |
|---|---|
| **Azure Function** (Timer trigger, Python) | Default choice. Managed Identity, secrets in Key Vault, timer schedule `0 */1 * * * *`. |
| **Container App Job** (schedule trigger) | Custom base image, private networking, or if the collector must live inside a VNet to reach a private Batch account. |
| **VM cron / Scheduled Task** | If an ops VM already exists and you want zero new resources. |

In production, replace `az account get-access-token` with the managed-identity token flow, and put the LAW shared key in Key Vault -- or move to the DCE / DCR ingestion API and get rid of the key entirely. Both changes are one-liners.

## Fields collected

For each node, per snapshot:

- `PoolId`, `NodeId`, `AccountName`
- `State`, `SchedulingState`, `StateTransitionTime`
- `VmSize`, `IpAddress`, `IsDedicated`
- `RunningTasks`, `TotalTasksRun`, `TotalTasksSucceeded`
- `AllocationTime`, `LastBootTime`
- `StartTaskState`, `StartTaskExitCode`, `StartTaskResult`
- `ErrorCount`, `ErrorCode`, `ErrorMessage` (first error only, message truncated to 500 chars)

## What this is NOT

This project is a *compensating monitoring layer*, not a workaround for the platform issue. If nodes get stuck because of a capacity constraint, a platform incident, or an image bug, the collector detects it earlier and with more detail -- it does not prevent it. Remediation still lives in your runbook (resize to 0 + resize back, reboot, reimage, ticket).

## License

MIT. See [LICENSE](LICENSE).