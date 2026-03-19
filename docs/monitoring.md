# Monitoring & Measurement

This document explains how the application measures pod scaling performance and detects problems in real time.

## The Two Measurement Systems

The application has two independent systems that count pods. They use different Kubernetes APIs on purpose — if one breaks, the other still works. If they disagree, that tells you something is wrong.

```mermaid
flowchart LR
    subgraph MONITOR["MONITOR (monitor.py)"]
        direction TB
        M_SRC["Data source:\nDeployment watch API\n(.status.readyReplicas)"]
        M_HOW["How it works:\nWatch threads get real-time\ndeployment status updates"]
        M_WHAT["What it measures:\nPods/second ready rate\nRolling average rate\nRate drop detection"]
        M_OUT["Output:\nrate_data.jsonl\n(one record per 5s tick)"]
        M_SRC --- M_HOW --- M_WHAT --- M_OUT
    end

    subgraph OBSERVER["OBSERVER (controller.py)"]
        direction TB
        O_SRC["Data source:\nPod list API\n(actual Running pods)"]
        O_HOW["How it works:\nPolls list_namespaced_pod\nevery 10 seconds"]
        O_WHAT["What it measures:\nActual Running pod count\nPending pod count"]
        O_OUT["Output:\nobserver.log\n(one record per 10s poll)"]
        O_SRC --- O_HOW --- O_WHAT --- O_OUT
    end

    style MONITOR fill:#0d3b66,stroke:#1d5a8e,color:#e0e0e0
    style OBSERVER fill:#14532d,stroke:#1a7a3a,color:#e0e0e0
```

## Monitor Deep Dive (monitor.py)

The monitor has three parts running concurrently:

### 1. Deployment Watch Threads

One background thread per namespace watches for deployment status changes. When Kubernetes updates a deployment's `readyReplicas` count, the watch thread receives the event immediately and updates the shared counters.

```mermaid
flowchart TB
    WATCH["Thread: _watch_deployments\n(stress-test)"] --> EV1["Receives:\ncpu-stress-test\nreadyReplicas=5000"]
    WATCH --> EV2["Receives:\nmemory-stress-test\nreadyReplicas=4800"]

    EV1 --> UPD1["Updates: _ns_counts\ncpu-stress-test = (5000, 1000, 6000)\nready / pending / total"]
    EV2 --> UPD2["Updates: _ns_counts\nmemory-stress-test = (4800, 1200, 6000)\nready / pending / total"]

    UPD1 --> RECOMP["_recompute() sums all deployments\n_ready=9800, _pending=2200, _total=12000"]
    UPD2 --> RECOMP

    style WATCH fill:#0d3b66,stroke:#1d5a8e,color:#e0e0e0
    style RECOMP fill:#14532d,stroke:#1a7a3a,color:#e0e0e0
```

### 2. Node Watch Thread

A single background thread watches for node additions and deletions. This tracks how many nodes Karpenter has provisioned.

### 3. Ticker Loop (async, 5s interval)

The ticker runs on the main async event loop. Every 5 seconds it:

1. Reads the current `_ready`, `_pending`, `_total` from the shared counters
2. Computes the rate: `(current_ready - previous_ready) / elapsed_seconds`
3. Checks for gaps (watch disconnects that caused stale data)
4. Checks for hold-at-peak (ready stable, pending zero — skip recording)
5. Records the data point to `rate_data.jsonl`
6. Checks if the rate dropped below the rolling average threshold
7. If rate dropped: fires an alert to the anomaly detector

### Rate Drop Detection

The monitor fires an alert when the instantaneous rate drops significantly below the rolling average. This catches sudden slowdowns caused by infrastructure problems.

```mermaid
flowchart TB
    AVG["Rolling average (last 30s)\n50 pods/sec"] --> CHECK{"Current rate\n5 pods/sec\n< 50 x 0.25 = 12.5 ?"}
    CHECK -->|"Yes"| ALERT["🚨 ALERT\nrate drop detected"]
    CHECK -->|"No"| OK["✅ Normal\nno alert"]

    ALERT --> SUPPRESS{"Suppressed?"}
    SUPPRESS -->|"> 95% of target ready"| SKIP1["Skip — rate naturally drops near target"]
    SUPPRESS -->|"Watch just reconnected"| SKIP2["Skip — gap data, not real slowdown"]
    SUPPRESS -->|"Alert already in flight"| SKIP3["Skip — avoid duplicate investigation"]
    SUPPRESS -->|"Hold-at-peak phase"| SKIP4["Skip — no pods being created"]
    SUPPRESS -->|"None of the above"| FIRE["Fire alert to\nAnomaly Detector"]

    style ALERT fill:#7c2d12,stroke:#a3441a,color:#e0e0e0
    style FIRE fill:#7c2d12,stroke:#a3441a,color:#e0e0e0
    style OK fill:#14532d,stroke:#1a7a3a,color:#e0e0e0
```

### Watch Reconnect Handling

At 30K pods, the K8s API is under heavy load. Watch connections break. When a watch thread reconnects, it does a full re-list of all deployments. This causes `_ready` to jump by thousands in one tick — which would produce a nonsensical rate like 2000 pods/sec.

The monitor detects this by tracking when re-lists happen (`_last_relist_time`). If a large delta coincides with a re-list, the data point is marked as a gap. Gap data points are still recorded (so you can see when disconnects happened) but they don't affect the rolling average or trigger alerts.

## Observer Deep Dive

The observer is simpler — it's a background thread that calls `list_namespaced_pod` every 10 seconds and counts pods by phase. It writes one line per poll to `observer.log`.

The observer exists solely for cross-validation. After the test, `_verify_run_data` compares the monitor's peak ready count against the observer's peak. If they disagree by more than 500 pods, it flags a verification issue.

## Health Sweep (health_sweep.py)

The health sweep runs once during hold-at-peak. Unlike the monitor (which tracks fleet-level rates), the health sweep identifies specific problematic nodes.

```mermaid
flowchart TB
    HS["Health Sweep"] --> AMP["AMP Queries (per-node)"]
    HS --> K8S["K8s Condition Check"]
    HS --> MERGE["Merge Results"]

    AMP --> CPU["CPU: avg by(node)\n(rate(node_cpu_seconds_total\n{mode=idle}[5m]))"]
    AMP --> MEM["Memory:\nnode_memory_MemAvailable_bytes\n/ node_memory_MemTotal_bytes"]
    AMP --> DISK["Disk:\nnode_filesystem_avail_bytes\n/ node_filesystem_size_bytes"]
    AMP --> NET["Network:\nrate(node_network_receive_errs_total[5m])"]
    AMP --> RESTART["Restarts:\nincrease(kube_pod_container\n_status_restarts_total[15m])"]

    K8S --> READY["Ready != True → NotReady"]
    K8S --> DISKP["DiskPressure == True"]
    K8S --> MEMP["MemoryPressure == True"]
    K8S --> PIDP["PIDPressure == True"]

    CPU --> MERGE
    MEM --> MERGE
    DISK --> MERGE
    NET --> MERGE
    RESTART --> MERGE
    READY --> MERGE
    DISKP --> MERGE
    MEMP --> MERGE
    PIDP --> MERGE

    MERGE --> RESULT["Per-node results:\nip-192-168-111-65: CPU 98%, MemoryPressure"]

    style HS fill:#0d3b66,stroke:#1d5a8e,color:#e0e0e0
    style AMP fill:#4a1d6e,stroke:#6b2fa0,color:#e0e0e0
    style K8S fill:#14532d,stroke:#1a7a3a,color:#e0e0e0
    style RESULT fill:#7c2d12,stroke:#a3441a,color:#e0e0e0
```

The key difference from the monitor: the health sweep returns **per-node** results. It tells you "node X has 98% CPU" not "the fleet average is 65%". This is how you find frozen or problematic nodes.

If AMP is not configured, the health sweep falls back to SSM — it runs shell commands on sampled nodes to check PSI (Pressure Stall Information), kubelet health, disk capacity, and per-core CPU utilization.

## Event Watcher (events.py)

The event watcher streams Kubernetes Warning events in real time via the watch API. One background thread per namespace. Events are written to `events.jsonl` as they arrive — no polling, no pagination.

The event watcher is separate from the monitor because events and pod counts are different data. Events tell you *why* something failed. Pod counts tell you *how many* failed. The anomaly detector uses both.

## Observability Scanner (observability.py)

The scanner is a proactive monitoring layer that runs alongside the monitor and event watcher. While the monitor tracks pod ready rate and the anomaly detector investigates after problems are detected, the scanner queries AMP/Prometheus and CloudWatch every 15-30 seconds looking for fleet-wide early warning signs (CPU pressure, pending pod backlogs, Karpenter queue depth, network errors).

When the scanner detects an issue, it writes the finding to an in-memory `SharedContext` (`shared_context.py`). If a rate drop alert fires shortly after, the anomaly detector checks the SharedContext first and can reference the scanner's finding instead of re-investigating from scratch.

See [docs/observability-scanner.md](observability-scanner.md) for the full query catalog, tiered investigation model, and cross-source correlation details.
