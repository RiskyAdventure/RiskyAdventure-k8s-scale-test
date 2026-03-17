# Monitoring & Measurement

This document explains how the application measures pod scaling performance and detects problems in real time.

## The Two Measurement Systems

The application has two independent systems that count pods. They use different Kubernetes APIs on purpose — if one breaks, the other still works. If they disagree, that tells you something is wrong.

```
┌──────────────────────────────────┐    ┌──────────────────────────────────┐
│          MONITOR                 │    │          OBSERVER                │
│                                  │    │                                  │
│  Data source:                    │    │  Data source:                    │
│    Deployment watch API          │    │    Pod list API                  │
│    (.status.readyReplicas)       │    │    (actual Running pods)         │
│                                  │    │                                  │
│  How it works:                   │    │  How it works:                   │
│    Watch threads get real-time   │    │    Polls list_namespaced_pod     │
│    deployment status updates     │    │    every 10 seconds              │
│                                  │    │                                  │
│  What it measures:               │    │  What it measures:               │
│    Pods/second ready rate        │    │    Actual Running pod count      │
│    Rolling average rate          │    │    Pending pod count             │
│    Rate drop detection           │    │                                  │
│                                  │    │                                  │
│  Output:                         │    │  Output:                         │
│    rate_data.jsonl               │    │    observer.log                  │
│    (one record per 5s tick)      │    │    (one record per 10s poll)     │
└──────────────────────────────────┘    └──────────────────────────────────┘
```

## Monitor Deep Dive (monitor.py)

The monitor has three parts running concurrently:

### 1. Deployment Watch Threads

One background thread per namespace watches for deployment status changes. When Kubernetes updates a deployment's `readyReplicas` count, the watch thread receives the event immediately and updates the shared counters.

```
Thread: _watch_deployments("stress-test")
  │
  ├── Receives: deployment/cpu-stress-test readyReplicas=5000
  │   └── Updates: _ns_counts["stress-test"]["cpu-stress-test"] = (5000, 1000, 6000)
  │                                                                 ready  pending total
  ├── Receives: deployment/memory-stress-test readyReplicas=4800
  │   └── Updates: _ns_counts["stress-test"]["memory-stress-test"] = (4800, 1200, 6000)
  │
  └── After each update: _recompute() sums all deployments
      └── _ready=9800, _pending=2200, _total=12000
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

```
Rolling average (last 30s): 50 pods/sec
Current rate:               5 pods/sec
Threshold:                  75% drop from average

5 < 50 × (1 - 0.75) = 12.5  →  ALERT: rate drop detected
```

The alert is suppressed when:
- We're near the target (>95% of target_pods ready) — rate naturally drops
- A watch reconnect just happened (gap data, not a real slowdown)
- Another alert is already being investigated
- We're in hold-at-peak (no pods being created)

### Watch Reconnect Handling

At 30K pods, the K8s API is under heavy load. Watch connections break. When a watch thread reconnects, it does a full re-list of all deployments. This causes `_ready` to jump by thousands in one tick — which would produce a nonsensical rate like 2000 pods/sec.

The monitor detects this by tracking when re-lists happen (`_last_relist_time`). If a large delta coincides with a re-list, the data point is marked as a gap. Gap data points are still recorded (so you can see when disconnects happened) but they don't affect the rolling average or trigger alerts.

## Observer Deep Dive

The observer is simpler — it's a background thread that calls `list_namespaced_pod` every 10 seconds and counts pods by phase. It writes one line per poll to `observer.log`.

The observer exists solely for cross-validation. After the test, `_verify_run_data` compares the monitor's peak ready count against the observer's peak. If they disagree by more than 500 pods, it flags a verification issue.

## Health Sweep (health_sweep.py)

The health sweep runs once during hold-at-peak. Unlike the monitor (which tracks fleet-level rates), the health sweep identifies specific problematic nodes.

```
Health Sweep
  │
  ├── AMP Queries (per-node)
  │   ├── CPU:     avg by(node)(rate(node_cpu_seconds_total{mode="idle"}[5m]))
  │   ├── Memory:  node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes
  │   ├── Disk:    node_filesystem_avail_bytes / node_filesystem_size_bytes
  │   ├── Network: rate(node_network_receive_errs_total[5m])
  │   └── Restarts: increase(kube_pod_container_status_restarts_total[15m])
  │
  ├── K8s Condition Check
  │   ├── Ready != True → NotReady
  │   ├── DiskPressure == True → disk issue
  │   ├── MemoryPressure == True → memory issue
  │   └── PIDPressure == True → process limit
  │
  └── Merge Results
      └── Per-node: "ip-192-168-111-65: CPU 98%, MemoryPressure"
```

The key difference from the monitor: the health sweep returns **per-node** results. It tells you "node X has 98% CPU" not "the fleet average is 65%". This is how you find frozen or problematic nodes.

If AMP is not configured, the health sweep falls back to SSM — it runs shell commands on sampled nodes to check PSI (Pressure Stall Information), kubelet health, disk capacity, and per-core CPU utilization.

## Event Watcher (events.py)

The event watcher streams Kubernetes Warning events in real time via the watch API. One background thread per namespace. Events are written to `events.jsonl` as they arrive — no polling, no pagination.

The event watcher is separate from the monitor because events and pod counts are different data. Events tell you *why* something failed. Pod counts tell you *how many* failed. The anomaly detector uses both.
