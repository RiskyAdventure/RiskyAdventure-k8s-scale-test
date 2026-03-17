# Configuration Reference

## CLI Flags

### Required

| Flag | Description |
|------|-------------|
| `--target-pods N` | How many pods to create. This is the total across all stressor deployments. |

### Cluster & Auth

| Flag | Default | Description |
|------|---------|-------------|
| `--kubeconfig PATH` | `~/.kube/config` | Path to your kubeconfig file |
| `--aws-profile NAME` | none | AWS CLI profile name for boto3 (EC2, SSM, CloudWatch, AMP) |
| `--flux-repo-path PATH` | — | Local path to the Flux Git repository containing deployment manifests |

### Test Behavior

| Flag | Default | Description |
|------|---------|-------------|
| `--auto-approve` | false | Skip all operator confirmation prompts (preflight, findings, cleanup) |
| `--pending-timeout SECS` | 600 | How long to wait for pending pods before declaring a timeout |
| `--hold-at-peak SECS` | 90 | How long to hold at peak pod count before starting cleanup |
| `--output-dir PATH` | `./scale-test-results` | Where to save run artifacts |
| `-v` / `--verbose` | false | Enable debug-level logging |

### Pod Distribution

| Flag | Default | Description |
|------|---------|-------------|
| `--stressor-weights JSON` | even split | JSON dict mapping deployment names to weights. Example: `'{"cpu-stress-test": 0.4, "memory-stress-test": 0.3}'` |
| `--exclude-apps LIST` | none | Comma-separated deployment names to skip |
| `--include-apps LIST` | none | Comma-separated deployment names to include (only these will scale) |

### Resource Sizing

| Flag | Default | Description |
|------|---------|-------------|
| `--cpu-limit-multiplier N` | 2.0 | CPU limit = request × this multiplier |
| `--memory-limit-multiplier N` | 1.5 | Memory limit = request × this multiplier |
| `--iperf3-server-ratio N` | 50 | How many client pods per iperf3 server (target_pods / ratio = server count) |

### ClusterLoader2

| Flag | Default | Description |
|------|---------|-------------|
| `--cl2-preload NAME` | `mixed-workload` | CL2 config template name from `configs/clusterloader2/` |
| `--cl2-timeout SECS` | 3600 | CL2 subprocess timeout |
| `--cl2-params PARAMS` | auto-computed | Override CL2 template parameters. Format: `NAMESPACES=10,DEPLOYMENTS_PER_NS=5` |

### Observability

| Flag | Default | Description |
|------|---------|-------------|
| `--amp-workspace-id ID` | none | Amazon Managed Prometheus workspace ID for health sweep queries |
| `--cloudwatch-log-group NAME` | none | CloudWatch log group for node-level logs |
| `--eks-cluster-name NAME` | none | EKS cluster name for observability checks |
| `--prometheus-url URL` | none | Direct Prometheus URL (alternative to AMP) |

## Example Commands

### Basic 10K test
```bash
python3 -m k8s_scale_test --target-pods 10000 --auto-approve -v
```

### Full 30K test with all observability
```bash
python3 -m k8s_scale_test \
  --target-pods 30000 \
  --auto-approve \
  --aws-profile "my-profile" \
  --flux-repo-path "/path/to/flux-repo" \
  --output-dir "./scale-test-results" \
  --cl2-preload mixed-workload \
  --hold-at-peak 120 \
  --pending-timeout 900 \
  --amp-workspace-id "ws-abc123" \
  --cloudwatch-log-group "/aws/containerinsights/my-cluster/dataplane" \
  --eks-cluster-name "my-cluster" \
  -v
```

### Custom pod distribution
```bash
python3 -m k8s_scale_test \
  --target-pods 20000 \
  --stressor-weights '{"cpu-stress-test": 0.5, "memory-stress-test": 0.3, "io-stress-test": 0.2}' \
  --exclude-apps "iperf3-client,sysctl-connection-test" \
  --auto-approve -v
```

## Known Issues KB Commands

```bash
# List all known failure patterns
k8s-scale-test kb list

# Search by keyword
k8s-scale-test kb search "IPAMD"
k8s-scale-test kb search "image pull"

# Show full details for a pattern
k8s-scale-test kb show ipamd-mac-collision

# Seed the KB with built-in patterns
k8s-scale-test kb seed

# Add a new pattern from a finding
k8s-scale-test kb add --from-finding path/to/finding.json
```
