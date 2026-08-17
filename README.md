# Azure Batch Node State Tracker

A small Python script that polls the Azure Batch REST API for per-node state and writes each snapshot into a Log Analytics custom table. It exists to answer a question the Azure Monitor metrics for Batch cannot: which node is stuck, in which pool, for how long, and why.

## Why it exists

The `Microsoft.Batch/batchAccounts` metric namespace publishes counts like `LeavingPoolNodeCount`, `UnusableNodeCount`, `StartTaskFailedNodeCount`, `RunningNodeCount`, and `IdleNodeCount`. Each one is an account-wide integer. There is no `nodeId` dimension and no `poolId` dimension. If three nodes are stuck in `leavingpool`, the metric tells you the count. It cannot tell you the node ID, the pool ID, when the state started, or the error code.

`AzureDiagnostics` ServiceLog does not fill the gap. It emits pool-level events such as `PoolResizeCompleteEvent` and `PoolAutoScaleEvent`, and task events. It does not emit a per-node state change record on the current schema.

The Batch REST API does return everything per node: `state`, `stateTransitionTime`, `errors[]`, `ipAddress`, `allocationTime`, `lastBootTime`, `startTaskInfo`. This script calls `GET /pools/{poolId}/nodes` on a schedule and lands each row in Log Analytics so it can be queried, alerted on, and joined with other tables.

## How it works

Each run:

1. Get an AAD token for `https://batch.core.windows.net/` using the Azure CLI (or a Managed Identity in production).
2. Call `GET /pools` and, for each pool, `GET /pools/{poolId}/nodes`, following `nextLink`.
3. Build one row per node with the fields listed under "Fields collected".
4. POST the batch of rows to the Log Analytics HTTP Data Collector API. The custom table is `BatchNodeInventory_CL`.

```
Azure Batch (pools, nodes)
        |
        v  REST poll every ~60s
   Collector (Python)
        |
        v  one POST per snapshot
   Log Analytics: BatchNodeInventory_CL
        |
        +--> KQL for on-call and dashboards
        +--> Log Analytics scheduled query rule (alerts)
```

## Quick start

Requires Python 3.9 or later and the Azure CLI.

```powershell
# Sign in to the subscription that owns the Batch account
az login
az account set --subscription "<your-subscription>"

# Fetch the Log Analytics shared key and drop it into %TEMP%\.law-key
az monitor log-analytics workspace get-shared-keys `
    -g <rg> -n <workspace> --query primarySharedKey -o tsv `
    > "$env:TEMP\.law-key"

# Point the collector at your workspace and account
$env:LAW_WORKSPACE_ID = "<workspace-customer-id-guid>"
$env:BATCH_ACCOUNT    = "<batch-account>"
$env:BATCH_REGION     = "westus2"

# Run one snapshot, or loop
python node-state-collector.py --once
python node-state-collector.py --loop --interval 60
```

A sample of what a snapshot looks like in `BatchNodeInventory_CL`:

| PoolId | NodeId | State | StateTransitionTime | IpAddress | ErrorCode |
|---|---|---|---|---|---|
| pool-cpu-dedicated | tvmps_4306456424a0e7e1... | rebooting | 2026-08-17T20:53:05Z | 10.60.1.4 |  |
| pool-spot | tvmps_729327cb38d16d74... | leavingpool | 2026-08-17T12:29:27Z | 10.60.1.6 |  |
| pool-autoscale | tvmps_93f33224f733877d... | starting | 2026-08-17T20:53:48Z | 10.60.1.11 |  |

## Queries

Per-node view with duration in current state:

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

Alert query. Use it as the source for a Log Analytics scheduled query rule that fires when any row is returned:

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

Per-pool live counts from the same table:

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

## Fields collected

Per node, per snapshot:

- `PoolId`, `NodeId`, `AccountName`
- `State`, `SchedulingState`, `StateTransitionTime`
- `VmSize`, `IpAddress`, `IsDedicated`
- `RunningTasks`, `TotalTasksRun`, `TotalTasksSucceeded`
- `AllocationTime`, `LastBootTime`
- `StartTaskState`, `StartTaskExitCode`, `StartTaskResult`
- `ErrorCount`, `ErrorCode`, `ErrorMessage` (first error only, message truncated to 500 characters)

## Running it in production

Common hosts:

- Azure Function on a Timer trigger. Managed Identity, shared key in Key Vault (or move to the DCR ingestion API and remove the key), cron like `0 */1 * * * *`.
- Container App Job on a schedule trigger. Use this when the collector must sit inside a VNet to reach a Batch account with a private endpoint, or when you want to ship a custom base image.
- A cron job or Scheduled Task on an existing ops VM. Zero new Azure resources.

For any of them, swap the CLI token call for a Managed Identity token, and move the LAW shared key out of `%TEMP%\.law-key`.

## What this does not do

This project is a monitoring layer. It observes state over time; it does not remediate. If a node is stuck because of a capacity constraint, a platform incident, or a bad image, the collector surfaces the node ID, the pool, the transition time, and the error code so on-call can act. The remediation runbook (resize the pool to 0 and back, reboot the node, reimage it, open a support ticket) still belongs to you.

## Notes

- Batch dataplane API version pinned in the script: `2025-06-01`.
- Log Analytics ingestion path used here is the classic HTTP Data Collector API. It is on a deprecation path in favour of the Logs Ingestion API (DCR/DCE); both work today, and the DCR path is the recommended production choice.

## License

MIT. See [LICENSE](LICENSE).