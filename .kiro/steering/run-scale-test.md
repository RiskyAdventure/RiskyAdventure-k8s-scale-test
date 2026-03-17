---
inclusion: manual
---

# Run Scale Test

When the user asks to run a scale test, follow this runbook exactly. Do not create a spec. This is an operational task, not a feature.

## Cluster Environment

- EKS cluster: `tf-shane`
- AWS profile: `shancor+test-Admin`
- Flux repo: `/Users/shancor/Projects/flux/flux2`
- AMP workspace: `ws-641df7c9-35d5-4cda-89e0-dc2dc873262d`
- CloudWatch log group: `/aws/containerinsights/tf-shane/dataplane`
- Output directory: `./scale-test-results`

## How to Launch

The scale test is a Python CLI at `src/k8s_scale_test`. It runs as a long-running background process.

### Command Template

```bash
nohup python3 -m k8s_scale_test \
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
  -v > scale-test-<TARGET_SHORT>.log 2>&1 &
echo "PID: $!"
```

Replace `<TARGET>` with the pod count (e.g., 30000) and `<TARGET_SHORT>` with a short label (e.g., `30k`).

### Default Target

If the user says "run a scale test" without specifying a pod count, ask them what target they want.

### Common Targets

| Target | Approx Nodes | Approx Duration |
|--------|-------------|-----------------|
| 10,000 | ~195 | ~12 min |
| 30,000 | ~215+ | ~30-45 min |

## Monitoring the Run

After launching, monitor the log file with `tail -50 scale-test-<TARGET_SHORT>.log` every 30-60 seconds. Watch for:

- Preflight GO/NO_GO decision
- Infrastructure pod scaling (iperf3 servers)
- Stressor pod scaling progress
- Anomaly alerts (rate drops, pending pods)
- CL2 preload status
- Hold-at-peak health sweep
- Cleanup and final summary

### Key Phases

1. **Preflight** — Validates cluster capacity (IPs, vCPU quota, NodePool limits, pod ceiling)
2. **Infrastructure scaling** — Deploys iperf3 servers first
3. **Stressor scaling** — Distributes target pods across cpu-stress, memory-stress, io-stress, iperf3-client, sysctl-connection-test
4. **CL2 preload** — Runs ClusterLoader2 mixed-workload concurrently
5. **Hold at peak** — Holds for `--hold-at-peak` seconds, runs health sweep
6. **Cleanup** — Scales all deployments back to 0, drains nodes

### Error Patterns to Watch

- `FailedCreatePodSandBox` — CNI/IPAMD IP allocation failures
- `FailedScheduling` — Insufficient resources or node affinity mismatches
- `ErrImagePull` / `ImagePullBackOff` — Image pull QPS throttling
- `FailedCreatePodContainer` — systemd cgroup timeout under load
- Rate drop alerts — Pod ready rate fell below threshold

## Results

Results are saved to `./scale-test-results/<run_id>/`. Key files:

- `summary.json` — Pass/fail, peak pod count, scaling steps, findings
- `chart.html` — Visual scaling timeline
- `preflight.json` — Capacity validation details
- `rate_data.jsonl` — Pod ready rate time series
- `events.jsonl` — Kubernetes events captured during the run
- `findings/` — Anomaly and diagnostic findings

## Optional Parameters

- `--stressor-weights '{"cpu-stress-test": 0.4, "memory-stress-test": 0.3, "io-stress-test": 0.2, "iperf3-client": 0.1}'` — Custom pod distribution weights
- `--exclude-apps "app1,app2"` — Skip specific deployments
- `--include-apps "app1,app2"` — Only scale specific deployments
- `--cl2-params "NAMESPACES=10,DEPLOYMENTS_PER_NS=5"` — Custom CL2 parameters
- `--pending-timeout 1200` — Increase timeout for larger runs
