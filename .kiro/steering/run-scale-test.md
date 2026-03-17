---
inclusion: manual
description: Comprehensive runbook for launching, monitoring, and investigating EKS scale tests
---

# Comprehensive Scale Test Runbook

When the user asks to run a scale test, follow this runbook. Do not create a spec. This is an operational task.

## Cluster Environment

- EKS cluster: `tf-shane`
- AWS profile: `shancor+test-Admin`
- Flux repo: `/Users/shancor/Projects/flux/flux2`
- AMP workspace: `ws-641df7c9-35d5-4cda-89e0-dc2dc873262d`
- CloudWatch log group: `/aws/containerinsights/tf-shane/dataplane`
- Output directory: `./scale-test-results`

## How to Launch

The scale test is a Python CLI at `src/k8s_scale_test`. It runs as a long-running process (30-45 min for 30K pods).

Launch using Kiro's `controlBashProcess` tool so output is captured and readable via `getProcessOutput`. Run from the repo root. The `-u` flag disables Python output buffering.

```bash
python3 -u -m k8s_scale_test \
  --target-pods <TARGET> \
  --auto-approve \
  --aws-profile "shancor+test-Admin" \
  --flux-repo-path "/Users/shancor/Projects/flux/flux2" \
  --output-dir "./scale-test-results" \
  --cl2-preload mixed-workload \
  --cl2-timeout 3600 \
  --hold-at-peak 120 \
  --pending-timeout 900 \
  --amp-workspace-id "ws-641df7c9-35d5-4cda-89e0-dc2dc873262d" \
  --cloudwatch-log-group "/aws/containerinsights/tf-shane/dataplane" \
  --eks-cluster-name "tf-shane" \
  --cpu-limit-multiplier 2.0 \
  --memory-limit-multiplier 1.5 \
  --iperf3-server-ratio 50 \
  -v
```

Replace `<TARGET>` with the pod count. If not specified, ask the user.

| Target | Approx Nodes | Approx Duration |
|--------|-------------|-----------------|
| 10,000 | ~195 | ~12 min |
| 30,000 | ~215+ | ~30-45 min |

### Optional Parameters

- `--stressor-weights '{"cpu-stress-test": 0.4, "memory-stress-test": 0.3, "io-stress-test": 0.2, "iperf3-client": 0.1}'`
- `--exclude-apps "app1,app2"` / `--include-apps "app1,app2"`
- `--cl2-params "NAMESPACES=10,DEPLOYMENTS_PER_NS=5"`
- `--pending-timeout 1200`


## Monitoring Pipeline — What Each Module Does

Six independent monitoring components run concurrently during the test. Each owns its own data path. Understand what each one does so you can verify it's working.

| Module | What it owns | Data source | Output |
|--------|-------------|-------------|--------|
| PodRateMonitor (`monitor.py`) | Pod ready rate measurement | K8s Deployment watch API | `rate_data.jsonl` |
| AnomalyDetector (`anomaly.py`) | Reactive investigation after alerts | K8s events, SSM, EC2 ENI, KB | `findings/*.json` |
| EventWatcher (`events.py`) | Real-time K8s event streaming | K8s watch API (Warning events) | `events.jsonl` |
| ObservabilityScanner (`observability.py`) | Proactive fleet-wide scans | AMP PromQL + CloudWatch Logs Insights | `scanner_findings.jsonl` |
| HealthSweepAgent (`health_sweep.py`) | Per-node health at hold-at-peak | Per-node PromQL or SSM fallback | `diagnostics/health_sweep.json` |
| Observer (in `controller.py`) | Independent pod count cross-check | K8s Pod list API | `observer.log` |


## Phase-by-Phase Verification

Use `getProcessOutput` to track progress. At each phase, verify the relevant modules are active.

### Phase 1: Preflight
- Watch for GO/NO_GO decision
- Check: subnet IPs, vCPU quota, NodePool limits, pod ceiling, AMP connectivity, CloudWatch Logs

### Phase 2: Infrastructure Scaling
- iperf3 servers deploy first
- Verify infrastructure pods reach Running before stressors start

### Phase 3: Stressor Scaling (most critical)
All monitoring modules are active. Verify each one:

1. **Monitor** — `rate_data.jsonl` should show data points every 5s. Watch for rate drop alerts.
2. **ObservabilityScanner** — Look for `Scanner [query_name]` log lines. Tier 1 (Prometheus) runs every 15-30s, Tier 2 (CloudWatch) every 60s when triggered. Scanner findings appear as WARNING-level log lines during scaling, not just at the end.
3. **EventWatcher** — `events.jsonl` should be growing with K8s Warning events.
4. **AnomalyDetector** — Fires on rate drops. Check for investigation pipeline output (K8s events → KB lookup → pod phase → stuck nodes → EC2 ENI → SSM).
5. **Observer** — `observer.log` should show pod counts every 10s.
6. **CL2 preload** — Runs concurrently. Watch for CL2 status messages.

### Phase 4: Hold at Peak
- Scanner switches to `Phase.HOLD_AT_PEAK` queries (memory pressure, CPU outliers)
- HealthSweepAgent runs per-node AMP queries (different from scanner's fleet-aggregated queries)
- Karpenter health check runs if InsufficientCapacityError was seen
- Verify health sweep completes within 60s timeout

### Phase 5: Cleanup
- All replicas set to 0
- CL2 namespaces deleted
- Scanner stops, findings included in summary
- Node drain (non-critical)

### Error Patterns to Watch

- `FailedCreatePodSandBox` — CNI/IPAMD IP allocation failures
- `FailedScheduling` — Insufficient resources or node affinity mismatches
- `ErrImagePull` / `ImagePullBackOff` — Image pull QPS throttling
- `FailedCreatePodContainer` — systemd cgroup timeout under load
- Rate drop alerts — Pod ready rate fell below threshold
- Scanner findings — Proactive detection of CPU pressure, pending backlogs, Karpenter queue depth, network errors


## Results

Results saved to `./scale-test-results/<run_id>/`:

| File | Contents |
|------|----------|
| `summary.json` | Pass/fail, peak pod count, scaling steps, findings, scanner_findings |
| `chart.html` | Interactive scaling timeline |
| `preflight.json` | Capacity validation details |
| `rate_data.jsonl` | Pod ready rate time series (one JSON per 5s tick) |
| `events.jsonl` | K8s Warning events |
| `scanner_findings.jsonl` | ObservabilityScanner proactive findings |
| `observer.log` | Independent pod count cross-check |
| `agent_context.json` | AI sub-agent context |
| `cl2_summary.json` | ClusterLoader2 results (if used) |
| `findings/*.json` | Anomaly investigation findings |
| `diagnostics/*.json` | Per-node SSM diagnostics |
| `diagnostics/health_sweep.json` | Node health sweep results |


## Investigation Guide

When something goes wrong during or after a test, follow this workflow.

### Step 0: Check Scanner Findings First

Read `scanner_findings.jsonl`. The ObservabilityScanner may have already detected the issue proactively before the anomaly detector's rate drop alert fired. Scanner findings include `query_name`, `severity`, `title`, `detail`, and `source`.

### Step 1: Query the Known Issues KB

Before manual investigation, check the KB for known patterns:

```bash
k8s-scale-test kb list
k8s-scale-test kb search "IPAMD"
k8s-scale-test kb show ipamd-mac-collision
```

12 seed entries cover the most common failures:

| entry_id | category | summary |
|---|---|---|
| `ipamd-mac-collision` | networking | VPC CNI MAC address collision at high pod density |
| `ipamd-ip-exhaustion` | networking | IPAMD IP/prefix allocation failures |
| `subnet-ip-exhaustion` | networking | Subnet-level IP exhaustion blocking ENI attachment |
| `coredns-bottleneck` | networking | CoreDNS overload causing DNS resolution failures |
| `karpenter-capacity` | capacity | Karpenter InsufficientCapacityError during scaling |
| `image-pull-throttle` | runtime | Container image pull QPS throttling |
| `oom-kill` | runtime | OOM kills and pod evictions from memory pressure |
| `disk-pressure` | storage | Node disk pressure causing pod evictions |
| `ec2-api-throttle` | networking | EC2 API rate limiting affecting VPC CNI operations |
| `nvme-disk-init` | storage | NVMe disk initialization failures on i4i instances |
| `kyverno-webhook-failure` | control-plane | Kyverno admission webhook blocking pod creation |
| `systemd-cgroup-timeout` | runtime | systemd cgroup setup timeouts during container creation |

If KB match score > 0.7, use the entry's root cause and recommended actions directly.

### Step 2: Read the Context File

Read `agent_context.json` from the evidence directory for:
- Cluster, namespaces, AMP workspace
- Current phase and `phase_start` timestamps (scope queries to relevant time window)
- Alerts that fired
- Existing findings (avoid duplicating work)

### Step 3: AMP Metric Investigation

Three metric exporters feed AMP:
- **node_exporter** (`node_*`) — Host-level: CPU, memory, disk, network per node
- **kube-state-metrics** (`kube_*`) — K8s object state: pod phases, deployment replicas, node conditions
- **cadvisor** (`container_*`) — Container-level resource usage

Anomalous patterns at scale:
- Sudden step changes rather than gradual increases
- Metrics diverging across AZs
- System component metrics (aws-node, kube-proxy) growing faster than workload metrics
- Metrics plateauing while pods are still being created (bottleneck)
- Oscillating patterns suggesting thrashing

### Step 4: CloudWatch Log Investigation

Key log sources in the dataplane log group:
- **kubelet** — Pod lifecycle, image pulls, evictions
- **containerd** — Container creation, snapshot errors
- **IPAMD (aws-node)** — IP allocation, ENI attachment
- **kube-proxy** — iptables/IPVS rule updates

Query strategy:
- Start broad around the alert timestamp, then narrow
- Filter for errors first, expand to warnings if needed
- Use affected node hostnames from AMP to filter log streams
- Look for the first occurrence of an error pattern (closer to root cause)

### Step 5: EKS Cluster Context

Use EKS MCP tools:
- `get_eks_insights` — Cluster-level health insights
- `list_k8s_resources` — Node conditions, pod states, system component health
- `get_k8s_events` — Most actionable failure information

During scaling, prioritize checking (in pod lifecycle order):
1. Are new nodes joining and becoming Ready? (Node provisioning)
2. Are pods getting IPs? (VPC CNI / IPAMD)
3. Are pods being scheduled? (Scheduler / resource constraints)
4. Are containers starting? (Runtime / image pull)
5. Are pods becoming Ready? (Application startup / health checks)

### Cross-Source Correlation

- Align time windows using `phase_start` from context file
- For alert investigations, center on alert timestamp ±3 minutes
- AMP range queries: 15s step for short windows, 60s for longer
- Let each finding guide the next query — don't follow a fixed checklist

### Prioritization (by impact on pod ready rate)

1. IP allocation failures
2. Node provisioning delays
3. Scheduler bottlenecks
4. Container runtime issues
5. Memory/CPU pressure
6. Control plane throttling


## Agent Finding Schema

When writing findings, save as `agent-{finding_id}.json` in `findings/`:

```json
{
  "finding_id": "agent-proactive-20240115T143500",
  "timestamp": "ISO 8601",
  "source": "proactive-scan | reactive-investigation | skeptical-review",
  "severity": "info | warning | critical",
  "title": "short summary",
  "description": "detailed explanation with evidence",
  "affected_resources": ["node names", "pod names"],
  "evidence": [
    {
      "source": "prometheus | cloudwatch | eks",
      "tool": "MCP tool name",
      "query": "query or parameters",
      "summary": "what the result showed"
    }
  ],
  "recommended_actions": ["next steps"]
}
```

Severity guidelines:
- **info** — Worth noting, not immediately actionable
- **warning** — Could escalate if not addressed
- **critical** — Actively causing or about to cause pod ready rate drops

Escalate when: affects pod ready rate directly, multiple correlated signals, spreading to more nodes.


## Skeptical Review Process

When reviewing a finding:

1. Identify the central claim
2. Re-query independently (different query approach if possible)
3. Check for staleness (conditions may have changed)
4. Look at what was NOT investigated
5. Query KB for matching known patterns
6. Assign confidence: high (confirmed), medium (partial), low (contradicted or gaps)
7. Write checkpoint questions if confidence < high
8. Append `review` field to the finding JSON

```json
{
  "review": {
    "confidence": "high | medium | low",
    "reasoning": "explanation",
    "alternative_explanations": ["other possible causes"],
    "checkpoint_questions": ["specific actionable questions"],
    "verification_results": [
      {"claim": "...", "verified": true, "detail": "..."}
    ]
  }
}
```
