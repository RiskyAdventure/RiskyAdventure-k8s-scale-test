---
inclusion: auto
description: Domain knowledge for investigating EKS scale test issues using AMP, CloudWatch, and EKS MCP tools
---

# EKS Scale Test Observability Guide

You are an AI sub-agent assisting with EKS scale test observability analysis. This guide provides domain knowledge for investigating fleet-wide issues during scale tests using AMP (Amazon Managed Prometheus), CloudWatch Logs, and EKS MCP tools.

## 0. Known Issues Knowledge Base — Query First

Before performing any manual investigation, **query the Known Issues KB**. The KB contains structured entries for known EKS scale test failure patterns with signatures, root causes, and recommended actions. Matching against the KB avoids redundant investigation of previously seen issues.

### How to Query the KB

**From the CLI:**
```bash
# List all known entries
k8s-scale-test kb list

# Search by keyword (case-insensitive match on title and root cause)
k8s-scale-test kb search "MAC collision"
k8s-scale-test kb search "IPAMD"

# Show full details for a specific entry
k8s-scale-test kb show ipamd-mac-collision
```

**Programmatically (during anomaly detection):**
```python
from k8s_scale_test.kb_store import KBStore
from k8s_scale_test.kb_matcher import SignatureMatcher

# KBStore is pre-loaded at controller startup with active entries in memory
entries = kb_store.load_all()          # zero-latency, from local cache
matcher = SignatureMatcher()
matches = matcher.match(finding_events, finding_diagnostics, entries)

if matches:
    best_entry, score = matches[0]
    # Use best_entry.root_cause and best_entry.recommended_actions
```

### Seed Entries in the KB

The KB ships with 12 seed entries covering the most common scale test failure patterns. These were migrated from this steering file's former inline pattern descriptions:

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

Run `k8s-scale-test kb show <entry_id>` for full signature details, root cause, recommended actions, and affected versions.

### Investigation Workflow

1. Collect K8s events from the affected time window
2. Query the KB for matches against those events (`kb search` or `SignatureMatcher.match()`)
3. If a match is found with score > 0.7, use the KB entry's root cause and recommended actions — skip SSM/EC2 evidence collection
4. If no match is found, proceed with the full investigation pipeline described in the sections below

## 1. EKS Scale Test Context

### What the Test Does

The scale test orchestrates rapid pod scaling on an EKS cluster to validate infrastructure behavior under load. The controller creates thousands of pods across multiple namespaces and monitors the pod ready rate — how quickly pods transition from Pending to Running to Ready.

### Test Phases

- **initializing** — Monitoring is being set up. No observability data worth scanning yet.
- **scaling** — Pods are being created. This is the highest-stress phase. Node autoscaling (Karpenter or Cluster Autoscaler) is actively provisioning. Network (VPC CNI / IPAMD) is allocating IPs. This is where most issues surface.
- **hold-at-peak** — All target pods are created, the system is at maximum load. This phase reveals sustained pressure issues: memory leaks, slow garbage collection, disk exhaustion, control plane throttling. This is your last chance to capture signal — once cleanup starts, pods scale down and evidence disappears.
- **cleanup** — Pods are being deleted. Not worth investigating unless cleanup itself is failing.
- **complete** — Test is done. Focus shifts to post-run analysis.

### Why Timing Matters

Signal is ephemeral during scale tests. Once pods scale down in cleanup, the metrics that showed the problem disappear. During scaling and hold-at-peak, you must capture evidence aggressively. If you see something anomalous, record it immediately — you may not be able to query it again 5 minutes later.

The `agent_context.json` file contains `phase_start` timestamps. Use these to scope your queries to the relevant time window. Don't query the last 24 hours when the scaling phase started 10 minutes ago.

### Reading the Context File

Before any investigation, read `agent_context.json` from the evidence directory. It tells you:
- What cluster, namespaces, and AMP workspace to target
- What phase the test is in (determines what to look for)
- Any alerts that have fired (rate drops that need investigation)
- Any existing findings from the anomaly detector (avoid duplicating work)

### ObservabilityScanner Findings

The controller now runs an `ObservabilityScanner` as a background task during scaling and hold-at-peak. Scanner findings are saved to `scanner_findings.jsonl` in the evidence directory and included in `summary.json` under the `scanner_findings` key. These are separate from anomaly detector findings.

The scanner runs two tiers of queries:
- **Tier 1 (Prometheus)** — Fleet-wide PromQL queries every 15-30s: node count growth, pending pods, CPU/memory pressure, Karpenter queue depth, network errors, disk pressure
- **Tier 2 (CloudWatch)** — Logs Insights queries every 60s, triggered when Tier 1 finds something: top error patterns in dataplane logs

When investigating, check `scanner_findings.jsonl` first — the scanner may have already detected the issue proactively before the anomaly detector's rate drop alert fired. Scanner findings include `query_name`, `severity`, `title`, `detail`, and `source` fields.

## 2. AMP Metric Patterns

### Available Metric Sources

At scale, three main metric exporters feed AMP:

- **node_exporter** — Host-level metrics: CPU, memory, disk, network per node. Metric names start with `node_`. These tell you about infrastructure health.
- **kube-state-metrics** — Kubernetes object state: pod phase counts, deployment replicas, node conditions. Metric names start with `kube_`. These tell you about orchestration health.
- **cadvisor** — Container-level resource usage: per-container CPU, memory, network. Metric names start with `container_`. These tell you about workload behavior.

### What to Look For

When scanning AMP, reason about patterns rather than checking fixed thresholds. What matters is the trend relative to the test phase:

- **CPU pressure** — Steady climb during scaling is normal; spikes on specific nodes or AZs are not. Watch for disproportionate system CPU (kernel time) and throttling on system-critical pods (kube-proxy, aws-node, coredns).
- **Memory pressure** — Fleet-wide drops in available memory, unbounded growth in system component memory, and OOM kills. Query the KB for `oom-kill` and `ipamd-ip-exhaustion` entries for known patterns.
- **Network signals** — IP allocation failures, DNS resolution latency spikes, network error counters. The KB covers the most common networking patterns — run `k8s-scale-test kb list` filtered by category `networking`.
- **Disk I/O** — Disk utilization climbing from image pulls, inode exhaustion, slow disk operations. See KB entries `disk-pressure` and `nvme-disk-init`.
- **Pod lifecycle** — Restart counts, pods stuck in Pending, increasing container creation time. Cross-reference with KB entries for the specific event reasons observed.

### Anomalous Patterns at Scale

Normal behavior during a scale test includes gradually increasing resource utilization. Anomalous patterns include:
- Sudden step changes rather than gradual increases
- Metrics diverging across availability zones (one AZ struggling while others are fine)
- System component metrics (aws-node, kube-proxy) growing faster than workload metrics
- Metrics plateauing while pods are still being created (suggests a bottleneck)
- Oscillating patterns (resource usage going up and down rapidly) suggesting thrashing


## 3. CloudWatch Log Patterns

### Log Sources

During EKS scale tests, the most informative CloudWatch log streams come from node-level system components. The log group name is provided in `agent_context.json` under `cloudwatch_log_group`.

Key log sources to investigate:

- **kubelet** — Pod lifecycle events, image pulls, volume mounts, eviction decisions.
- **containerd** — Container creation, start, stop events. Snapshot errors and resource exhaustion.
- **IPAMD (aws-node)** — IP allocation, ENI attachment, warm pool management.
- **kube-proxy** — iptables/IPVS rule updates and sync duration.

### Querying for Known Patterns

Rather than manually scanning for error strings, query the KB first. Each KB entry contains `log_patterns` (regex patterns) in its signature that match the relevant log lines. For example:

```bash
# Find KB entries relevant to a specific log message
k8s-scale-test kb search "failed to allocate IP"
k8s-scale-test kb search "FailedCreatePodSandBox"
```

If the KB returns a match, use the entry's root cause and recommended actions directly.

### Query Strategy for Unknown Patterns

When the KB has no match and you need to investigate manually:
- Start with a broad time window around the alert, then narrow down
- Filter for error-level messages first, then expand to warnings if needed
- Use affected node hostnames from AMP analysis to filter log streams
- Look for the first occurrence of an error pattern — that's often closer to the root cause than the flood of subsequent errors
- Correlate error timestamps with the rate drop alert timestamp from the context file

## 4. EKS Cluster Context

### Control Plane Health

The EKS control plane is managed by AWS, but you can observe its health through:

- **API server responsiveness** — If the Kubernetes API is slow or returning errors, everything downstream suffers. Check for API server latency metrics or timeout errors in component logs.
- **etcd performance** — At high object counts, etcd can become a bottleneck. Look for slow list/watch operations or leader election changes.
- **Scheduler throughput** — During rapid scaling, the scheduler must place thousands of pods. Check for scheduling latency increases or unschedulable pod backlogs.

Use the EKS MCP tools to check:
- `get_eks_insights` — Provides cluster-level health insights and recommendations
- `list_k8s_resources` — Check node conditions, pod states, and system component health
- `get_k8s_events` — Kubernetes events often contain the most actionable information about failures

### Addon Status

EKS addons that matter during scale tests:

- **VPC CNI (aws-node)** — Must be healthy for pod networking. Check its DaemonSet status — are all pods running? Any restarts?
- **CoreDNS** — Must scale with pod count. Check if CoreDNS pods are overloaded or if the deployment has enough replicas.
- **kube-proxy** — Must update routing rules as pods come and go. Check for sync delays.
- **Karpenter / Cluster Autoscaler** — If node autoscaling is in use, check provisioning status. Are new nodes joining the cluster fast enough?

### What Matters During Scaling

During the scaling phase, prioritize checking:
1. Are new nodes joining and becoming Ready? (Node provisioning bottleneck)
2. Are pods getting IPs? (VPC CNI / IPAMD bottleneck)
3. Are pods being scheduled? (Scheduler bottleneck or resource constraints)
4. Are containers starting? (Runtime or image pull bottleneck)
5. Are pods becoming Ready? (Application startup or health check issues)

This is roughly the order of the pod lifecycle, and issues at each stage have different root causes. Work through them in order when investigating a rate drop.

## 5. Agent Finding JSON Schema

When writing findings, you MUST follow this exact JSON structure. Each finding is saved as `agent-{finding_id}.json` in the `findings/` subdirectory of the run directory.

### Required Fields

```json
{
  "finding_id": "string — unique identifier, e.g. agent-proactive-20240115T143500",
  "timestamp": "string — ISO 8601 timestamp of when the finding was produced",
  "source": "string — one of: proactive-scan, reactive-investigation, skeptical-review",
  "severity": "string — one of: info, warning, critical",
  "title": "string — short summary of the finding (one line)",
  "description": "string — detailed explanation with evidence and reasoning",
  "affected_resources": ["string — node names, pod names, or other resource identifiers"],
  "evidence": [
    {
      "source": "string — prometheus, cloudwatch, or eks",
      "tool": "string — the MCP tool name used",
      "query": "string — the query or parameters used",
      "summary": "string — what the result showed"
    }
  ],
  "recommended_actions": ["string — suggested next steps for the operator"]
}
```

### Optional Review Field

The `review` field is added by the skeptical review agent after the initial finding is written. When present, it MUST have this structure:

```json
{
  "review": {
    "confidence": "string — one of: high, medium, low",
    "reasoning": "string — explanation of why this confidence level was assigned",
    "alternative_explanations": [
      "string — other possible causes the investigation may have missed"
    ],
    "checkpoint_questions": [
      "string — questions for the operator, present when confidence is medium or low"
    ],
    "verification_results": [
      {
        "claim": "string — the specific claim being verified",
        "verified": "string or boolean — true, false, or partial",
        "detail": "string — what the independent verification found"
      }
    ]
  }
}
```

### Severity Guidelines

- **info** — Observation worth noting but not immediately actionable. Example: "CPU utilization is 60% across the fleet, trending upward."
- **warning** — Issue that could escalate if not addressed. Example: "Memory pressure on 12 nodes in us-west-2b, approaching eviction thresholds."
- **critical** — Active issue causing or about to cause pod ready rate drops. Example: "IPAMD failing to allocate IPs on 8 nodes, 350 pods stuck in Pending."

Escalate severity when:
- The issue affects pod ready rate directly
- Multiple correlated signals point to the same root cause
- The issue is spreading (more nodes affected over time)

### Finding ID Convention

Use this format for finding IDs:
- Proactive scans: `agent-proactive-{ISO8601 timestamp}`
- Reactive investigations: `agent-reactive-{ISO8601 timestamp}`
- Skeptical reviews update the existing finding — they don't create new files


## 6. Investigation Strategies

### Cross-Source Correlation

The most valuable findings come from correlating signals across AMP, CloudWatch Logs, and EKS state. Here's how to approach it:

1. **Check scanner findings first.** Read `scanner_findings.jsonl` from the evidence directory. The ObservabilityScanner may have already detected the issue proactively during scaling or hold-at-peak. If a scanner finding matches the alert, use it as your starting point — it includes the raw query result and severity assessment.

2. **Query the KB next.** Before any manual investigation, check if the K8s events or log patterns match a known KB entry. This can resolve the issue instantly.

3. **Start with the broadest signal.** If the KB has no match and you're doing a proactive scan, start with AMP fleet-wide metrics to identify which nodes or components are under stress. If you're investigating an alert, start with the alert details (timestamp, rate, affected counts).

3. **Narrow to affected resources.** Once you identify affected nodes from AMP metrics, use those node hostnames to filter CloudWatch Logs. Look for error patterns on those specific nodes around the same time window.

4. **Check cluster-level context.** Use EKS MCP tools to see if the issue is reflected in Kubernetes events, node conditions, or addon status. Sometimes the K8s event stream has the most direct explanation.

5. **Follow the evidence dynamically.** Don't follow a fixed checklist. If AMP shows memory pressure, check CloudWatch for OOM kills. If CloudWatch shows IPAMD errors, check AMP for IP allocation metrics. Let each finding guide your next query.

### Time Window Alignment

When correlating across sources, align your time windows carefully:
- Use the `phase_start` timestamp from the context file as your baseline
- For alert investigations, center your query window on the alert timestamp with ±3 minutes
- AMP range queries should use step intervals appropriate to the time window (15s for short windows, 60s for longer ones)
- CloudWatch Logs Insights queries should use the same time window as your AMP queries

### Prioritization

Focus on issues most likely to cause pod ready rate drops. In rough priority order:

1. IP allocation failures
2. Node provisioning delays
3. Scheduler bottlenecks
4. Container runtime issues
5. Memory/CPU pressure
6. Control plane throttling

For detailed root causes and recommended actions for each category, query the KB: `k8s-scale-test kb list` or `k8s-scale-test kb search "<category>"`.

### When to Escalate Severity

Escalate from info to warning when:
- A metric is trending toward a known failure threshold
- Multiple nodes show the same pattern simultaneously
- The rate of change is accelerating

Escalate from warning to critical when:
- The issue is actively causing pod failures or rate drops
- The issue is spreading to more nodes
- Correlated signals from multiple sources confirm the same root cause

### Evidence Capture

When you find something, capture it thoroughly:
- Record the exact query you ran and what it returned
- Note the time window and which nodes/pods were affected
- Include both the raw observation and your interpretation
- If a metric is trending, note the direction and rate of change

Remember: signal disappears after cleanup. Capture everything during scaling and hold-at-peak.

## 7. Skeptical Review Process

The skeptical review exists to catch runaway reasoning — where an early mistake in the investigation propagates through subsequent analysis steps, leading to a confident but wrong conclusion.

### Independent Verification

When reviewing a finding:

1. **Pick the most important claim.** Every finding has a central claim (e.g., "memory pressure on 12 nodes is causing pod failures"). Identify it.

2. **Re-query independently.** Don't trust the investigation agent's evidence at face value. Run your own query against the MCP servers to verify the central claim. Use a slightly different query approach if possible — for example, if the investigation used a range query, try an instant query at the peak timestamp.

3. **Check for staleness.** The investigation may have queried data that has since changed. Verify that the conditions described still hold (or note that they've resolved).

4. **Look at what was NOT investigated.** The investigation agent may have focused on one hypothesis and ignored contradicting evidence. Check adjacent metrics and logs that the investigation didn't query.

### Confidence Level Assignment

- **high** — Your independent verification confirms the key claims. The evidence is consistent across sources. The root cause explanation is plausible and supported by data.
- **medium** — Partial confirmation. Some claims check out but others are ambiguous. The evidence supports the conclusion but doesn't rule out alternatives. Or the investigation missed relevant context.
- **low** — Your verification contradicts the finding, key data is missing, or the reasoning chain has a logical gap. The conclusion may still be correct, but the evidence doesn't adequately support it.

### Identifying Alternative Explanations

For every finding, ask:
- What else could cause these symptoms?
- Is there a simpler explanation that the investigation overlooked?
- Could the observed effect be a symptom of a different root cause?
- Does the KB contain a known pattern that matches these symptoms? Run `k8s-scale-test kb search` with relevant keywords.
- Could the issue be environmental (AZ-specific, instance-type-specific) rather than systemic?

Check the source code and documentation in the repository for context the investigation agent might not have considered. The codebase may contain known limitations, workarounds, or configuration details that explain the observed behavior.

### Writing Checkpoint Questions

When confidence is medium or low, write checkpoint questions that:
- Are specific and actionable (not vague "should we investigate more?")
- Point the operator toward the information gap that would resolve the ambiguity
- Suggest a concrete next step (e.g., "Run `k8s-scale-test kb show ipamd-mac-collision` to check if this matches the known VPC CNI MAC collision pattern")
- Highlight where the investigation's reasoning might have gone wrong

### Review Workflow

1. Read the finding completely before starting verification
2. Identify the central claim and the evidence chain supporting it
3. Query the KB for known patterns matching the finding's symptoms
4. Run at least one independent MCP query to verify the central claim
5. Assess whether the evidence actually supports the conclusion (vs. just being correlated)
6. Consider alternative explanations based on domain knowledge and KB entries
7. Assign confidence level with clear reasoning
8. Write checkpoint questions if confidence is below high
9. Append the `review` field to the finding JSON
