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

### Tracing

| Flag | Default | Description |
|------|---------|-------------|
| `--enable-tracing` / `--no-enable-tracing` | enabled | Enable OpenTelemetry tracing (exports to X-Ray via ADOT OTLPAwsSpanExporter with SigV4) |

### Knowledge Base Infrastructure

These flags apply to both `run` and `kb` subcommands:

| Flag | Default | Description |
|------|---------|-------------|
| `--kb-table NAME` | `scale-test-kb` | DynamoDB table name for KB storage |
| `--kb-bucket NAME` | none | S3 bucket for KB entry storage |
| `--kb-prefix PREFIX` | `kb-entries/` | S3 key prefix for KB entries |

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

# List only pending (unreviewed) entries
k8s-scale-test kb list --pending

# Search by keyword
k8s-scale-test kb search "IPAMD"
k8s-scale-test kb search "image pull"

# Show full details for a pattern
k8s-scale-test kb show ipamd-mac-collision

# Seed the KB with built-in patterns
k8s-scale-test kb seed

# Add a new pattern manually
k8s-scale-test kb add \
  --title "My New Pattern" \
  --category networking \
  --root-cause "Description of the root cause" \
  --event-reasons "FailedCreatePodSandBox,FailedScheduling" \
  --log-patterns "error pattern one,error pattern two" \
  --severity warning

# Ingest findings from a completed test run
k8s-scale-test kb ingest path/to/scale-test-results/2026-03-17_18-52-18

# Approve a pending entry
k8s-scale-test kb approve ipamd-mac-collision

# Remove an entry
k8s-scale-test kb remove ipamd-mac-collision

# Set up DynamoDB table and verify S3 bucket
k8s-scale-test kb setup --kb-table scale-test-kb --kb-bucket my-bucket
```
