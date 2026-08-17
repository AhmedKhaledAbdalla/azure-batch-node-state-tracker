"""
Azure Batch Node State Collector.

Solves a real customer problem: Azure Batch platform metrics report
`leavingPoolVmCount`, `unusableNodeCount`, `startTaskFailedNodeCount`, etc.
as ACCOUNT-WIDE totals with NO `nodeId` dimension. As a result:

  * The portal cannot tell you WHICH node is stuck, or FOR HOW LONG.
  * AzureDiagnostics ServiceLog does not emit per-node state-change events.
  * Log Analytics cannot answer "which nodes have been in state X > 1 hour?"

This collector polls the Azure Batch REST API on a schedule, extracts the
per-node fields the platform never exposes (`stateTransitionTime`, `errors`,
`allocationTime`, `lastBootTime`) and ships them to a Log Analytics custom
table `BatchNodeInventory_CL`. That gives customers:

  * Per-node history: `state`, `schedulingState`, transitions over time
  * Duration in current state (from `stateTransitionTime`)
  * Cause: `errors[0].code` / `errors[0].message` for `unusable` nodes
  * Provisioning time: `LastBootTime - AllocationTime`

Use `--once` for a single snapshot or `--loop --interval 60` as a daemon.
Run under Managed Identity in production (Azure Function, Container App Job).
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.request

# ---------- configuration -----------------------------------------------------
WORKSPACE_ID = os.environ.get("LAW_WORKSPACE_ID", "50ea7a52-c6ef-4f2b-b04c-6896af6f2be3")
BATCH_ACCOUNT = os.environ.get("BATCH_ACCOUNT", "batchgrafus389488")
BATCH_REGION = os.environ.get("BATCH_REGION", "westus2")
BATCH_URL = f"https://{BATCH_ACCOUNT}.{BATCH_REGION}.batch.azure.com"
BATCH_API = "2025-06-01"
LOG_TYPE = "BatchNodeInventory"


def key_from_temp() -> str:
    if "LAW_SHARED_KEY" in os.environ:
        return os.environ["LAW_SHARED_KEY"]
    with open(os.path.join(os.environ["TEMP"], ".law-key"), "r", encoding="utf-8") as f:
        return f.read().strip()
"""
    Retrieves the Log Analytics Workspace shared key.
    Checks the 'LAW_SHARED_KEY' environment variable first,
    falling back to a local temporary file if not set.
    
    Returns:
        str: The Log Analytics Workspace shared key.
"""


def aad_token(resource: str) -> str:
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", resource, "-o", "json"],
        capture_output=True, text=True, check=True, shell=True,
    )
    return json.loads(out.stdout)["accessToken"]
"""
    Obtains an Azure Active Directory (Entra ID) access token using Azure CLI.
    
    Args:
        resource (str): The target Azure resource URL (e.g., Batch endpoint).
        
    Returns:
        str: The JWT access token for authentication.
"""


def batch_get(path: str, token: str) -> list[dict]:
    url = f"{BATCH_URL}{path}?api-version={BATCH_API}"
    items: list[dict] = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.load(r)
        items.extend(body.get("value", []))
        url = body.get("odata.nextLink") or body.get("nextLink")
    return items
"""
    Sends a GET request to the Azure Batch REST API and handles pagination.
    Loops through 'nextLink' if the results span multiple pages.
    
    Args:
        path (str): The API endpoint path (e.g., '/pools').
        token (str): The Bearer access token for authorization.
        
    Returns:
        list[dict]: A complete list of JSON objects returned by the API.
"""

def law_sign(cid: str, key: str, date: str, length: int) -> str:
    to_hash = f"POST\n{length}\napplication/json\nx-ms-date:{date}\n/api/logs"
    hashed = hmac.new(base64.b64decode(key), to_hash.encode("utf-8"), hashlib.sha256).digest()
    return f"SharedKey {cid}:{base64.b64encode(hashed).decode()}"
"""
    Generates the HMAC-SHA256 authorization signature required by the 
    Log Analytics HTTP Data Collector API.
    
    Args:
        cid (str): The Log Analytics Workspace ID.
        key (str): The Workspace shared key.
        date (str): The current date in RFC 1123 format.
        length (int): The byte length of the request body.
        
    Returns:
        str: The formatted 'SharedKey' authorization string.
"""


def post_to_law(rows: list[dict]) -> None:
    if not rows:
        return
    body = json.dumps(rows).encode("utf-8")
    date = dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    sig = law_sign(WORKSPACE_ID, key_from_temp(), date, len(body))
    url = f"https://{WORKSPACE_ID}.ods.opinsights.azure.com/api/logs?api-version=2016-04-01"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "content-type": "application/json",
        "Authorization": sig,
        "Log-Type": LOG_TYPE,
        "x-ms-date": date,
        "time-generated-field": "TimeGenerated",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        if r.status >= 300:
            raise RuntimeError(f"LAW ingest failed: {r.status} {r.read()!r}")
"""
    Formats the collected node data as JSON and securely sends it 
    to the Azure Log Analytics Workspace via a POST request.
    
    Args:
        rows (list[dict]): A list of dictionaries representing node state snapshots.
        
    Raises:
        RuntimeError: If the Log Analytics API returns an HTTP error status (>= 300).
"""


def snapshot() -> tuple[int, int]:
    token = aad_token("https://batch.core.windows.net/")
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    pools = batch_get("/pools", token)
    rows: list[dict] = []
    for p in pools:
        try:
            nodes = batch_get(f"/pools/{p['id']}/nodes", token)
        except Exception as e:
            print(f"warn: nodes for {p['id']}: {e}", file=sys.stderr)
            continue
        for n in nodes:
            errors = n.get("errors") or []
            start = n.get("startTaskInfo") or {}
            rows.append({
                "TimeGenerated": now,
                "AccountName": BATCH_ACCOUNT,
                "PoolId": p["id"],
                "NodeId": n.get("id"),
                "State": n.get("state"),
                "SchedulingState": n.get("schedulingState"),
                "StateTransitionTime": n.get("stateTransitionTime"),
                "VmSize": n.get("vmSize"),
                "IpAddress": n.get("ipAddress"),
                "IsDedicated": n.get("isDedicated"),
                "RunningTasks": n.get("runningTasksCount", 0),
                "TotalTasksRun": n.get("totalTasksRun", 0),
                "TotalTasksSucceeded": n.get("totalTasksSucceeded", 0),
                "AllocationTime": n.get("allocationTime"),
                "LastBootTime": n.get("lastBootTime"),
                "StartTaskState": start.get("state") or "",
                "StartTaskExitCode": start.get("exitCode"),
                "StartTaskResult": start.get("result") or "",
                "ErrorCount": len(errors),
                "ErrorCode": (errors[0].get("code") if errors else ""),
                "ErrorMessage": (errors[0].get("message") or "")[:500] if errors else "",
            })
    post_to_law(rows)
    return len(pools), len(rows)
"""
    The core orchestration function. 
    Iterates through all Azure Batch pools and their respective nodes, 
    extracts granular state and error information not exposed by default metrics, 
    and ships the formatted data to Log Analytics.
    
    Returns:
        tuple[int, int]: A tuple containing (number_of_pools_processed, number_of_nodes_processed).
"""

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()

    def run() -> None:
        pools, nodes = snapshot()
        stamp = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"[{stamp}] pools={pools} nodes={nodes}")

    if args.loop:
        while True:
            try:
                run()
            except Exception as e:
                print(f"error: {e}", file=sys.stderr)
            time.sleep(args.interval)
    else:
        run()


if __name__ == "__main__":
    main()
