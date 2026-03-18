# Anomaly Detection & Investigation

This document explains how the application investigates problems when they're detected during a scale test.

## When Investigation Triggers

The anomaly detector doesn't run continuously. It activates when the monitor fires one of these alerts:

| Alert Type | Trigger | What it means |
|-----------|---------|---------------|
| `RATE_DROP` | Pod ready rate dropped >75% below rolling average | Something is slowing down pod creation |
| `PENDING_TIMEOUT` | Pods still pending after timeout (default 900s) | Pods can't be scheduled or started |
| `MONITOR_GAP` | Watch reconnect caused a blind spot in monitoring | K8s API connection broke under load |

## Investigation Pipeline

When an alert fires, the anomaly detector runs a multi-layer evidence collection pipeline. Each layer adds more detail. The layers run in order because later layers use results from earlier ones.

```
Alert received
  │
  ▼
Layer 0: SharedContext Lookup ────────────────────────────────────
  │  Query the in-memory SharedContext for recent scanner findings
  │  that overlap the alert's time window (±correlation_window,
  │  default 120s). If the ObservabilityScanner already detected
  │  the issue proactively, the anomaly detector references it
  │  instead of re-collecting the same data.
  │
  │  Matches are classified as "strong" (temporal + resource
  │  overlap) or "weak" (temporal only).
  │
  ▼
Layer 1: K8s Events ──────────────────────────────────────────────
  │  Collect all Warning events from the target namespaces.
  │  This is the starting point — events tell you what K8s
  │  itself thinks went wrong.
  │
  │  Example: FailedCreatePodSandBox=210, FailedScheduling=5864
  │
  ▼
Layer 2: KB Lookup ───────────────────────────────────────────────
  │  Check if the events match a known failure pattern in the
  │  Knowledge Base. If there's a match with score > 0.7,
  │  return the known root cause immediately — skip layers 3-6.
  │
  │  This saves 10-15 seconds of SSM/EC2 calls for known issues.
  │
  ▼
Layer 3: Pod Phase Breakdown ─────────────────────────────────────
  │  Count pods by phase: Pending vs ContainerCreating vs Running.
  │  This distinguishes scheduling problems (Pending) from
  │  container startup problems (ContainerCreating).
  │
  │  Example: Pending=20453, Running=1648
  │
  ▼
Layer 4: Stuck Pod Nodes + Conditions ────────────────────────────
  │  Find which nodes have Pending pods. Group by node to
  │  identify the worst-affected nodes. Also check node
  │  conditions (NotReady, DiskPressure, MemoryPressure).
  │
  │  Example: 20 nodes with stuck pods, 0 with bad conditions
  │
  │  After this layer, Layer 0's scanner findings are matched
  │  against the alert's affected resources (stuck nodes, event
  │  objects, namespaces) to classify strong vs weak matches.
  │
  ▼
Layer 4.5: AMP Metric Query ─────────────────────────────────────
  │  Query AMP/Prometheus for node-level metrics: CPU utilization,
  │  memory utilization, network error rates, IPAMD metrics, and
  │  pod restart counts. Uses the AMPMetricCollector from
  │  health_sweep.py (SigV4 signing, concurrent PromQL execution).
  │
  │  Only runs when an AMPMetricCollector is configured. Threshold
  │  violations (CPU > 90%, memory > 90%, network errors > 0,
  │  pod restarts > 5) are added to evidence and root cause.
  │
  ▼
Layer 5: EC2 ENI State ───────────────────────────────────────────
  │  For the top 10 affected nodes, query EC2 for their network
  │  interface state: how many ENIs, how many IPv4 prefixes,
  │  which subnet, how many IPs left in the subnet.
  │
  │  Nodes with 0 prefixes = IPAMD failed to allocate IPs.
  │  Subnets with < 100 IPs = subnet exhaustion.
  │
  ▼
Layer 6: SSM Node Diagnostics ───────────────────────────────────
  │  For the top 3 affected nodes, run shell commands via SSM
  │  to collect logs and system state. The commands are chosen
  │  dynamically based on what layers 1-5 found:
  │
  │  If FailedCreatePodSandBox → collect IPAMD log, CNI config
  │  If image pull failures    → check disk space, image list
  │  If evictions/OOM          → check /proc/meminfo, dmesg
  │  If FailedScheduling       → check kubelet config
  │  If InvalidDiskCapacity    → check NVMe disk state
  │
  ▼
Correlate ────────────────────────────────────────────────────────
  │  Combine all evidence into a Finding with:
  │  - Severity (info / warning / critical)
  │  - Root cause (human-readable explanation)
  │  - Affected resources (node names, pod names)
  │  - Evidence references (what was checked)
  │  - Scanner correlation (strong/weak matches from Layer 0)
  │  - AMP metric evidence (violations or clean checks)
  │
  └──► Save to findings/{finding_id}.json
       │
       ▼
  Skeptical Review (async, non-blocking) ─────────────────────────
       The FindingReviewer independently re-verifies the finding
       by re-querying AMP/CloudWatch with different query approaches,
       checking for staleness, assigning confidence (high/medium/low),
       listing alternative explanations, and generating checkpoint
       questions. The review is appended to the finding JSON.
```

## Dynamic SSM Command Selection

The SSM commands aren't hardcoded. The `_pick_ssm_commands` method looks at what the K8s events and EC2 data revealed, then chooses which commands to run. This avoids wasting time collecting irrelevant data.

```python
# Example: events show FailedCreatePodSandBox and EC2 shows 0 prefixes
# → Run IPAMD log collection and CNI config check

# Example: events show Evicted and OOMKilling
# → Run memory check and PSI (Pressure Stall Information)

# Example: events show FailedScheduling but nothing else
# → Run kubelet config check and mpstat (per-core CPU)
```

## Root Cause Extraction

The `_extract_root_cause` method scans all collected evidence and builds a human-readable explanation. It looks for specific patterns:

| Evidence | Root Cause |
|----------|-----------|
| InsufficientCapacityError events | Karpenter can't provision nodes (spot exhaustion or NodePool limit) |
| FailedCreatePodSandBox > 10 | VPC CNI failures (likely MAC collision at high density) |
| Nodes with 0 ENI prefixes | IPAMD failed to allocate IPs |
| Subnet available IPs < 100 | Subnet IP exhaustion |
| IPAMD log shows "rate exceeded" | EC2 API throttling |
| IPAMD log shows "accessdenied" | IAM permission issue on node role |
| dmesg shows OOM kills | Memory exhaustion on node |
| PSI avg10 > 25% for CPU | CPU contention (processes waiting for CPU time) |
| mpstat shows cores with idle < 10% | Specific CPU cores saturated |
| AMP: CPU > 90% on node | Node CPU saturation (from AMP/Prometheus) |
| AMP: Memory > 90% on node | Node memory pressure (from AMP/Prometheus) |
| AMP: Network errors > 0/s on node | CNI or VPC networking issues (from AMP/Prometheus) |
| AMP: Pod restarts > 5 on node | Crashloops or OOM kills on node (from AMP/Prometheus) |
| Scanner pre-detected: {title} | ObservabilityScanner found the issue before the alert fired |

## Severity Assessment

| Severity | Criteria |
|----------|---------|
| CRITICAL | Nodes with 0 ENI prefixes, OR >10 stuck nodes, OR >50 warning events |
| WARNING | Some stuck nodes, OR >10 warning events |
| INFO | Events present but no clear impact |

## Known Issues KB

The KB is checked in Layer 2, before the expensive SSM/EC2 calls. It contains 12 built-in patterns covering the most common scale test failures. Each entry has:

- **Signature**: Event reasons and log patterns that identify this issue
- **Root cause**: What's actually happening
- **Recommended actions**: How to fix it
- **Affected versions**: Which VPC CNI / Karpenter versions are affected

When the KB matches with score > 0.7, the investigation short-circuits — no SSM commands, no EC2 API calls. The known root cause and actions are returned directly.
