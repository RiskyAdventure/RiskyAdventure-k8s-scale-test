# Test Lifecycle

This document walks through every phase of a scale test run, in order. Each phase explains what happens, what can go wrong, and what the output looks like.

## Phase Diagram

```
  ┌───────────┐    ┌──────────┐    ┌───────────┐    ┌──────────┐
  │ Preflight │───►│  Infra   │───►│ Stressor  │───►│ Hold at  │
  │  Checks   │    │ Scaling  │    │  Scaling  │    │   Peak   │
  └───────────┘    └──────────┘    └───────────┘    └──────────┘
                                                         │
                                                         ▼
                                        ┌──────────┐    ┌──────────┐
                                        │  Node    │◄───│ Cleanup  │
                                        │  Drain   │    │          │
                                        └──────────┘    └──────────┘
```

## Phase 1: Preflight Checks

**What happens:** Before creating any pods, the controller validates that the cluster has enough capacity. It checks:

- **Subnet IPs** — Are there enough IP addresses in the VPC subnets for all the pods? Each pod needs one IP.
- **vCPU Quota** — Does the AWS account have enough EC2 vCPU headroom to launch the nodes we'll need?
- **NodePool CPU Limits** — Does the Karpenter NodePool have enough CPU budget for the workload?
- **Pod Ceiling** — Can the nodes physically fit this many pods? (maxPods per node × max nodes)
- **Observability** — Can we reach AMP (Prometheus), CloudWatch Logs, and the EKS API? These are needed for monitoring during the test.

**Output:** `preflight.json` with all capacity numbers and a GO/NO_GO decision.

**What can go wrong:** If any check fails, the controller logs a NO_GO with the specific constraint that's blocking. The operator can review and decide whether to proceed anyway.

```
Preflight: max_achievable=30492, target=30000, decision=go
  Subnet IPs: 114135 available, 30000 needed — OK
  vCPU quota: 9267 headroom, ~6520 needed — OK
  NodePool CPU: 9766 limit, 6420 needed — OK
  Pod ceiling: 30492 effective, target 30000 — OK
  AMP connectivity: OK
  CloudWatch Logs: OK (log group exists)
  Observability: 3/3 checks passed
```

## Phase 2: Infrastructure Scaling

**What happens:** Before creating the main stressor pods, the controller deploys infrastructure pods first — specifically iperf3 servers. These need to be running before the iperf3 client stressor pods start, because the clients need servers to connect to.

The controller:
1. Writes the iperf3-server replica count to the Flux repo
2. Commits and pushes to Git
3. Waits for Flux to reconcile (up to 60s timeout, then relies on auto-sync)
4. Polls until all infrastructure pods are Running (up to 5 minutes)

**What can go wrong:**
- Flux reconciliation timeout (common, non-blocking — Flux auto-syncs)
- ECR image pull throttling (transient, resolves via backoff)
- Karpenter provisioning delays (nodes take 1-2 minutes to join)

## Phase 3: Stressor Scaling

**What happens:** This is the main event. The controller distributes the target pod count across 5 stressor deployments:

```
cpu-stress-test:        6,000 replicas
memory-stress-test:     6,000 replicas
io-stress-test:         6,000 replicas
iperf3-client:          6,000 replicas
sysctl-connection-test: 6,000 replicas
─────────────────────────────────
Total:                 30,000 pods
```

The controller:
1. Writes all replica counts to the Flux repo in one commit
2. Pushes to Git
3. Starts the monitor, observer, and event watcher
4. Enters a polling loop (every 5s) tracking ready/pending counts
5. Runs diagnostics once after 60s if pods are still pending
6. Waits until all pods are ready OR the pending timeout expires

**Concurrent activity during this phase:**
- CL2 (ClusterLoader2) creates additional K8s objects (namespaces, deployments, services, configmaps, secrets) to simulate a realistic cluster workload
- The monitor tracks pod ready rate and fires alerts on rate drops
- The event watcher streams K8s Warning events to `events.jsonl`
- The observer independently counts Running pods every 10s
- The ObservabilityScanner runs fleet-wide PromQL queries every 15-30s (CPU, memory, pending pods, Karpenter queue, network errors, disk pressure) and CloudWatch Logs Insights queries every 60s when Prometheus findings exist

**What can go wrong:**
- `FailedScheduling` — Not enough nodes yet (Karpenter is still provisioning)
- `FailedCreatePodSandBox` — VPC CNI can't allocate IPs (IPAMD issue)
- `ErrImagePull` / `ImagePullBackOff` — ECR pull rate limit exceeded
- `InvalidDiskCapacity` — NVMe disk not initialized on new i4i nodes
- `InsufficientCapacityError` — EC2 can't launch the requested instance type
- Monitor watch disconnects — K8s API under load, watch stream breaks

## Phase 4: Hold at Peak

**What happens:** Once all target pods are running, the controller holds for a configurable period (default 120s). During this time:

1. **Health sweep** runs — queries AMP for per-node CPU, memory, disk, network errors, and pod restarts. Also checks K8s node conditions (Ready, DiskPressure, MemoryPressure, PIDPressure). Identifies specific nodes that are struggling.
2. **Karpenter health check** runs (if InsufficientCapacityError was seen) — checks the Karpenter controller pod for restarts and resource limits.

**Why hold?** The scaling phase is a burst — everything is changing rapidly. The hold phase reveals sustained pressure issues: memory leaks, slow garbage collection, disk exhaustion, control plane throttling. These only show up when the system is at steady-state load.

**What can go wrong:**
- AMP queries fail (auth issues, endpoint unreachable)
- Nodes go NotReady during hold (spot interruptions, resource exhaustion)
- Karpenter is resource-starved (CPU/memory limits causing throttling)

## Phase 5: Cleanup

**What happens:** The controller scales everything back to zero:

1. Sets all deployment replicas to 0 in the Flux repo
2. Commits and pushes
3. Waits for Flux to reconcile
4. Tracks pod deletion rate (typically 100-250 pods/sec)
5. Deletes CL2 namespaces (375 namespaces with all their objects)
6. Generates the summary, chart, and verification report
7. Saves everything to the evidence store

## Phase 6: Node Drain

**What happens:** After cleanup, the controller deletes Karpenter NodeClaims so the nodes are terminated. This is non-critical and can take several minutes as EC2 instances shut down.

## Output Files

After a complete run, the evidence directory contains:

| File | What it contains |
|------|-----------------|
| `summary.json` | Pass/fail, peak pod count, scaling rate, findings, validity |
| `chart.html` | Interactive HTML chart of the scaling timeline |
| `preflight.json` | All capacity validation details |
| `config.json` | Snapshot of the run configuration |
| `rate_data.jsonl` | Pod ready rate time series (one JSON object per 5s tick) |
| `events.jsonl` | All K8s Warning events captured during the run |
| `observer.log` | Independent pod count cross-validation data |
| `agent_context.json` | Context file for AI sub-agent integration |
| `cl2_summary.json` | ClusterLoader2 results (if CL2 was used) |
| `scanner_findings.jsonl` | ObservabilityScanner proactive findings (one JSON line per finding) |
| `findings/*.json` | Anomaly detection findings with evidence |
| `diagnostics/*.json` | Per-node SSM diagnostic data |
| `diagnostics/health_sweep.json` | Node health sweep results |
