---
inclusion: auto
---

# EKS Scale Test Observability Guide

You are an AI sub-agent assisting with EKS scale test observability analysis. This guide provides domain knowledge for investigating fleet-wide issues during scale tests using AMP (Amazon Managed Prometheus), CloudWatch Logs, and EKS MCP tools.

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

## 2. AMP Metric Patterns

### Available Metric Sources

At scale, three main metric exporters feed AMP:

- **node_exporter** — Host-level metrics: CPU, memory, disk, network per node. Metric names start with `node_`. These tell you about infrastructure health.
- **kube-state-metrics** — Kubernetes object state: pod phase counts, deployment replicas, node conditions. Metric names start with `kube_`. These tell you about orchestration health.
- **cadvisor** — Container-level resource usage: per-container CPU, memory, network. Metric names start with `container_`. These tell you about workload behavior.

### What to Look For

When scanning AMP, reason about patterns rather than checking fixed thresholds. What matters is the trend relative to the test phase:

**CPU pressure signals:**
- Node CPU utilization climbing steadily during scaling — normal if gradual, concerning if it spikes on specific nodes or availability zones
- System CPU (kernel time) disproportionately high compared to user CPU — suggests kernel-level contention (network stack, cgroup operations)
- CPU throttling on system-critical pods (kube-proxy, aws-node, coredns) — these can cascade into broader failures

**Memory pressure signals:**
- Available memory dropping across many nodes simultaneously — could indicate a workload memory issue or a system component leak
- IPAMD (aws-node) memory growing without bound — known issue at high pod density, the VPC CNI plugin can leak memory when allocating/deallocating many IPs rapidly
- OOM kills appearing in node metrics — check which processes are being killed (workload vs system)

**Network signals:**
- IP allocation failures or delays — the VPC CNI needs to attach ENIs and allocate IPs; at scale this can hit EC2 API rate limits or subnet exhaustion
- DNS resolution latency spikes — CoreDNS can become a bottleneck when thousands of pods start simultaneously
- Network error counters rising on specific nodes — could indicate ENI attachment issues or security group evaluation delays

**Disk I/O signals:**
- Disk utilization climbing on nodes — container image pulls at scale can saturate disk I/O
- inode exhaustion — many small containers can exhaust inodes before disk space runs out
- Slow disk operations correlating with pod startup delays

**Pod lifecycle signals:**
- Pod restart counts increasing — indicates containers are crash-looping
- Pods stuck in Pending state — scheduling failures, resource constraints, or node pressure
- Container creation time increasing — suggests containerd or image pull bottlenecks

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

- **kubelet** — The primary node agent. Logs pod lifecycle events, image pulls, volume mounts, and eviction decisions. During scale tests, look for pod sandbox creation failures, image pull backoff, and eviction warnings.
- **containerd** — The container runtime. Logs container creation, start, and stop events. At scale, look for slow container creation, snapshot errors, and resource exhaustion messages.
- **IPAMD (aws-node)** — The VPC CNI plugin's IP address management daemon. Logs IP allocation, ENI attachment, and warm pool management. This is often the first component to show stress during scale tests because every new pod needs an IP.
- **kube-proxy** — Manages iptables/IPVS rules for service routing. At high pod counts, rule updates can become slow and log warnings about sync duration.

### Common Error Patterns

When querying CloudWatch Logs, look for these patterns correlated with the alert timestamp (use a window of ±2-3 minutes around the alert):

**IPAMD / VPC CNI errors:**
- "failed to allocate IP" or "no available IP addresses" — subnet exhaustion or ENI limits reached
- "failed to attach ENI" — EC2 API throttling or ENI limit per instance type
- "warm pool" warnings — the CNI's IP warm pool is depleted faster than it can refill
- "ipamd" with "error" or "timeout" — general IPAMD health issues

**kubelet errors:**
- "FailedCreatePodSandBox" — pod networking setup failed, often tied to IPAMD issues
- "FailedScheduling" — scheduler couldn't place the pod (resource constraints, affinity rules, taints)
- "Evicted" or "eviction" — node is under resource pressure and evicting pods
- "OOMKilled" — a container exceeded its memory limit
- "ImagePullBackOff" or "ErrImagePull" — container image pull failures, often due to registry throttling at scale
- "NodeNotReady" — the node is reporting unhealthy, which blocks all pod scheduling on it

**containerd errors:**
- "failed to create containerd task" — runtime-level failure creating the container
- "context deadline exceeded" — operations timing out under load
- "snapshot" errors — filesystem layer issues during image unpacking

**General patterns:**
- Correlate error timestamps with the rate drop alert timestamp from the context file
- Look for error bursts (many errors in a short window) rather than isolated errors
- Check if errors are concentrated on specific nodes or spread across the fleet
- Filter by node hostname when you've identified affected nodes from AMP metrics

### Query Strategy

When using CloudWatch Logs Insights:
- Start with a broad time window around the alert, then narrow down
- Filter for error-level messages first, then expand to warnings if needed
- Use the affected node hostnames from AMP analysis to filter log streams
- Look for the first occurrence of an error pattern — that's often closer to the root cause than the flood of subsequent errors

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

1. **Start with the broadest signal.** If you're doing a proactive scan, start with AMP fleet-wide metrics to identify which nodes or components are under stress. If you're investigating an alert, start with the alert details (timestamp, rate, affected counts).

2. **Narrow to affected resources.** Once you identify affected nodes from AMP metrics, use those node hostnames to filter CloudWatch Logs. Look for error patterns on those specific nodes around the same time window.

3. **Check cluster-level context.** Use EKS MCP tools to see if the issue is reflected in Kubernetes events, node conditions, or addon status. Sometimes the K8s event stream has the most direct explanation.

4. **Follow the evidence dynamically.** Don't follow a fixed checklist. If AMP shows memory pressure, check CloudWatch for OOM kills. If CloudWatch shows IPAMD errors, check AMP for IP allocation metrics. Let each finding guide your next query.

### Time Window Alignment

When correlating across sources, align your time windows carefully:
- Use the `phase_start` timestamp from the context file as your baseline
- For alert investigations, center your query window on the alert timestamp with ±3 minutes
- AMP range queries should use step intervals appropriate to the time window (15s for short windows, 60s for longer ones)
- CloudWatch Logs Insights queries should use the same time window as your AMP queries

### Prioritization

Focus on issues most likely to cause pod ready rate drops. In rough priority order:

1. **IP allocation failures** — Pods can't start without IPs. IPAMD issues are the #1 cause of rate drops at scale.
2. **Node provisioning delays** — If Karpenter or Cluster Autoscaler can't add nodes fast enough, pods queue up in Pending.
3. **Scheduler bottlenecks** — At very high pod counts, the scheduler itself can become a bottleneck.
4. **Container runtime issues** — containerd failures or image pull throttling slow pod startup.
5. **Memory/CPU pressure** — Resource exhaustion leads to evictions and scheduling failures.
6. **Control plane throttling** — API server rate limiting can slow everything down.

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
- Are there known issues (IPAMD memory leaks, CoreDNS scaling limits, ENI attachment delays) that match the pattern?
- Could the issue be environmental (AZ-specific, instance-type-specific) rather than systemic?

Check the source code and documentation in the repository for context the investigation agent might not have considered. The codebase may contain known limitations, workarounds, or configuration details that explain the observed behavior.

### Writing Checkpoint Questions

When confidence is medium or low, write checkpoint questions that:
- Are specific and actionable (not vague "should we investigate more?")
- Point the operator toward the information gap that would resolve the ambiguity
- Suggest a concrete next step ("Check if these nodes are running VPC CNI v1.12 which has the known IPAMD leak")
- Highlight where the investigation's reasoning might have gone wrong

Good checkpoint questions:
- "The finding blames memory pressure, but 4 nodes also show CPU core saturation. Should we investigate CPU contention as the primary cause?"
- "Are these nodes running the VPC CNI version with the known IPAMD memory leak at high pod density?"
- "The investigation found errors on 3 nodes, but the rate drop affected 350 pods. Are there additional affected nodes not captured in this finding?"

Bad checkpoint questions:
- "Is this finding correct?" (too vague)
- "Should we look at more data?" (not actionable)
- "Are there other issues?" (not specific)

### Review Workflow

1. Read the finding completely before starting verification
2. Identify the central claim and the evidence chain supporting it
3. Run at least one independent MCP query to verify the central claim
4. Assess whether the evidence actually supports the conclusion (vs. just being correlated)
5. Consider alternative explanations based on domain knowledge
6. Assign confidence level with clear reasoning
7. Write checkpoint questions if confidence is below high
8. Append the `review` field to the finding JSON
