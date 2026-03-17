# k8s-scale-test

A Kubernetes scale testing framework that orchestrates rapid pod scaling on EKS clusters, monitors pod ready rates in real time, detects anomalies, and produces structured evidence for post-run analysis.

Built for validating EKS infrastructure behavior at 10K–30K+ pod scale — node autoscaling (Karpenter), VPC CNI/IPAMD, scheduler throughput, and container runtime performance.

## Documentation

| Document | What it covers |
|----------|---------------|
| [Architecture](docs/architecture.md) | System overview, module map, data flow diagrams, design decisions |
| [Test Lifecycle](docs/test-lifecycle.md) | Step-by-step walkthrough of every test phase with diagrams |
| [Monitoring](docs/monitoring.md) | How pod rate measurement works, monitor vs observer, health sweep |
| [Anomaly Detection](docs/anomaly-detection.md) | Investigation pipeline, SSM command selection, root cause extraction |
| [Configuration](docs/configuration.md) | All CLI flags, example commands, KB commands |
| [MCP Setup](docs/mcp-setup.md) | Prometheus, CloudWatch, and EKS MCP server configuration |

## How It Works

The test controller follows a GitOps workflow via Flux:

1. **Preflight** validates cluster capacity (subnet IPs, vCPU quota, NodePool limits, pod ceiling) and checks observability connectivity (AMP, CloudWatch, EKS API)
2. **Infrastructure scaling** deploys iperf3 servers first, waits for them to be ready
3. **Stressor scaling** distributes target pods across cpu-stress, memory-stress, io-stress, iperf3-client, and sysctl-connection-test deployments by committing replica counts to the Flux repo
4. **CL2 preload** runs ClusterLoader2 concurrently to create additional K8s objects (namespaces, deployments, services, configmaps, secrets) proportional to the target pod count
5. **Monitoring** tracks pod ready rate via K8s deployment watch API, with an independent observer using the pod list API for cross-validation
6. **Anomaly detection** investigates rate drops and pending timeouts by collecting K8s events, pod phase breakdowns, node diagnostics via SSM, and ENI state
7. **Hold at peak** runs a node health sweep, checks Karpenter resource usage
8. **Cleanup** scales all deployments to 0, deletes CL2 namespaces, drains nodes

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Run a 10K pod scale test
python3 -m k8s_scale_test \
  --target-pods 10000 \
  --auto-approve \
  --aws-profile "your-profile" \
  --flux-repo-path "/path/to/flux-repo" \
  --output-dir "./scale-test-results" \
  -v
```

## Prerequisites

- Python 3.9+
- `kubectl` configured with cluster access
- AWS credentials with EC2, EKS, SSM, and CloudWatch permissions
- A Flux-managed Git repository with stress test deployment manifests
- [ClusterLoader2](https://github.com/kubernetes/perf-tests/tree/master/clusterloader2) binary (optional, for CL2 preload)

## CLI Reference

### Scale Test

```
python3 -m k8s_scale_test --target-pods <N> [options]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--target-pods` | required | Target pod count |
| `--auto-approve` | false | Skip operator confirmation prompts |
| `--aws-profile` | none | AWS CLI profile for boto3 session |
| `--flux-repo-path` | — | Path to the Flux Git repository |
| `--output-dir` | `./scale-test-results` | Where to write run artifacts |
| `--pending-timeout` | 600 | Seconds before declaring a timeout |
| `--hold-at-peak` | 90 | Seconds to hold at peak before cleanup |
| `--cl2-preload` | `mixed-workload` | CL2 config template name |
| `--cl2-timeout` | 3600 | CL2 subprocess timeout in seconds |
| `--cl2-params` | auto-computed | Override CL2 params (e.g., `NAMESPACES=10,DEPLOYMENTS_PER_NS=5`) |
| `--stressor-weights` | even split | JSON dict of deployment weights |
| `--cpu-limit-multiplier` | 2.0 | CPU limit as multiplier of request |
| `--memory-limit-multiplier` | 1.5 | Memory limit as multiplier of request |
| `--iperf3-server-ratio` | 50 | Client pods per iperf3 server |
| `--amp-workspace-id` | none | AMP workspace ID for health sweep |
| `--cloudwatch-log-group` | none | CloudWatch log group for node logs |
| `--eks-cluster-name` | none | EKS cluster name for observability |
| `--kubeconfig` | `~/.kube/config` | Path to kubeconfig |
| `--prometheus-url` | none | Direct Prometheus URL (alternative to AMP) |
| `-v` | false | Verbose logging |

### Known Issues KB

The tool includes a knowledge base of known EKS scale test failure patterns.

```bash
# List all entries
k8s-scale-test kb list

# Search by keyword
k8s-scale-test kb search "IPAMD"

# Show full details
k8s-scale-test kb show ipamd-mac-collision

# Seed the KB with built-in entries
k8s-scale-test kb seed
```

## Output

Each run produces a timestamped directory under `--output-dir`:

```
scale-test-results/2026-03-17_18-52-18/
├── summary.json          # Pass/fail, peak pod count, scaling rate, findings
├── chart.html            # Interactive scaling timeline visualization
├── preflight.json        # Capacity validation details
├── config.json           # Run configuration snapshot
├── rate_data.jsonl       # Pod ready rate time series (5s intervals)
├── events.jsonl          # Kubernetes events captured during the run
├── observer.log          # Independent pod count cross-validation
├── agent_context.json    # Context file for AI sub-agent integration
├── cl2_summary.json      # ClusterLoader2 results (if CL2 preload was used)
├── findings/             # Anomaly detection findings
│   └── finding-*.json
└── diagnostics/          # Node-level SSM diagnostics and health sweep
    ├── health_sweep.json
    └── <node-name>_<timestamp>.json
```

## Architecture

```
src/k8s_scale_test/
├── cli.py              # CLI entry point and argument parsing
├── controller.py       # Main orchestration — preflight, scaling, monitoring, cleanup
├── monitor.py          # Real-time pod ready rate tracking via K8s deployment watch
├── anomaly.py          # Anomaly detection — correlates events, metrics, diagnostics
├── preflight.py        # Capacity validation and observability connectivity checks
├── health_sweep.py     # Node health sweep via AMP/Prometheus and K8s conditions
├── infra_health.py     # Karpenter controller health checks
├── flux.py             # Flux repo reader/writer for GitOps manifest management
├── evidence.py         # Evidence store — persists all run artifacts
├── events.py           # K8s event watcher
├── diagnostics.py      # Node diagnostics via SSM (kubelet, containerd, IPAMD logs)
├── metrics.py          # Node metrics analysis via Prometheus
├── chart.py            # HTML chart generation from rate data
├── cl2_parser.py       # ClusterLoader2 result parser
├── agent_context.py    # Context file writer for AI sub-agent integration
├── models.py           # Data models (dataclasses) for all structured data
├── kb_store.py         # Known Issues KB storage (DynamoDB + S3)
├── kb_matcher.py       # Signature matching against KB entries
├── kb_ingest.py        # Auto-ingestion of findings into KB
└── kb_seed.py          # Seed KB with built-in failure patterns
```

## Monitoring & Observability

The test controller has two independent monitoring paths:

- **Monitor** (deployment watch API) — tracks `readyReplicas` from deployment status updates in real time, computes rolling average ready rate, fires alerts on rate drops
- **Observer** (pod list API) — polls actual Running pod count every 10s as an independent cross-check, catches deployment controller lag or watch disconnects

### MCP Server Integration

For AI-assisted investigation during scale tests, the tool integrates with three AWS MCP servers:

| MCP Server | Purpose | What It Provides |
|------------|---------|-----------------|
| `awslabs.prometheus-mcp-server` | Fleet-wide metrics | Node CPU/memory, pod counts, Karpenter metrics via PromQL |
| `awslabs.cloudwatch-mcp-server` | Node-level logs | Kubelet, containerd, IPAMD, kube-proxy logs via Logs Insights |
| `awslabs.eks-mcp-server` | Cluster state | Node conditions, pod events, EKS insights |

See [docs/mcp-setup.md](docs/mcp-setup.md) for configuration details.

## Testing

```bash
# Run all tests
python3 -m pytest tests/ -v

# Run with coverage
python3 -m pytest tests/ --cov=k8s_scale_test --cov-report=term-missing
```

The test suite includes property-based tests via Hypothesis for the preflight capacity calculations and CL2 parser.

## Known Scale Test Patterns

The KB ships with 12 seed entries covering common failure patterns:

| Pattern | Category | Description |
|---------|----------|-------------|
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

## License

Internal use only.
