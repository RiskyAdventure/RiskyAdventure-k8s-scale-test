# MCP Server Setup for AWS Observability Integration

This guide covers how to configure the three AWS MCP (Model Context Protocol) servers that the AI sub-agent uses during EKS scale tests. These servers give the Kiro agent access to Amazon Managed Prometheus (AMP), CloudWatch Logs, and EKS cluster state — enabling proactive monitoring and reactive investigation without hardcoded queries.

## Prerequisites

- **uv** — The Python package manager. All three MCP servers are run via `uvx` (the tool runner bundled with `uv`). Install it from [astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/).
- **AWS credentials** — The MCP servers use your ambient AWS credentials (environment variables, `~/.aws/credentials`, or IAM role). Ensure you have read access to AMP, CloudWatch Logs, and EKS in the target region.

## Configuration

Add the following entries to `~/.kiro/settings/mcp.json`. If the file already has an `mcpServers` object, merge these entries into it.

```json
{
  "mcpServers": {
    "awslabs.prometheus-mcp-server": {
      "command": "uvx",
      "args": ["awslabs.prometheus-mcp-server"],
      "env": {
        "PROMETHEUS_URL": "https://aps-workspaces.us-west-2.amazonaws.com/workspaces/{workspace_id}",
        "AWS_REGION": "us-west-2"
      }
    },
    "awslabs.cloudwatch-mcp-server": {
      "command": "uvx",
      "args": ["awslabs.cloudwatch-mcp-server"],
      "env": {
        "AWS_REGION": "us-west-2"
      }
    },
    "awslabs.eks-mcp-server": {
      "command": "uvx",
      "args": ["awslabs.eks-mcp-server"],
      "env": {
        "AWS_REGION": "us-west-2",
        "EKS_CLUSTER_NAME": "{cluster_name}"
      }
    }
  }
}
```

Replace the placeholder values before use:
- `{workspace_id}` — Your AMP workspace ID (e.g., `ws-abc123def456`)
- `{cluster_name}` — Your EKS cluster name (e.g., `my-scale-test-cluster`)
- `us-west-2` — Your target AWS region

## MCP Server Details

### awslabs.prometheus-mcp-server

Provides tools to query Amazon Managed Prometheus (AMP) via PromQL. This is the primary data source for fleet-wide metric analysis during scale tests.

**Tools provided:**

| Tool | Description |
|------|-------------|
| `ExecuteQuery` | Run an instant PromQL query against AMP |
| `ExecuteRangeQuery` | Run a range PromQL query over a time window |
| `ListMetrics` | List available metric names in the AMP workspace |
| `GetAvailableWorkspaces` | List AMP workspaces in the region |
| `GetServerInfo` | Get AMP server metadata |

**Environment variables:**

| Variable | Required | Description |
|----------|----------|-------------|
| `PROMETHEUS_URL` | Yes | Full URL to the AMP workspace endpoint. Format: `https://aps-workspaces.{region}.amazonaws.com/workspaces/{workspace_id}` |
| `AWS_REGION` | Yes | AWS region where the AMP workspace is located |

### awslabs.cloudwatch-mcp-server

Provides tools for CloudWatch Logs analysis and CloudWatch Metrics. Used during reactive investigations to correlate log errors with metric anomalies.

**Tools provided:**

| Tool | Description |
|------|-------------|
| `execute_log_insights_query` | Run a CloudWatch Logs Insights query |
| `get_logs_insight_query_results` | Retrieve results from a Logs Insights query |
| `analyze_log_group` | Analyze a log group for patterns and anomalies |
| `get_metric_data` | Retrieve CloudWatch metric data points |
| `get_active_alarms` | List currently active CloudWatch alarms |
| `cancel_logs_insight_query` | Cancel a running Logs Insights query |

**Environment variables:**

| Variable | Required | Description |
|----------|----------|-------------|
| `AWS_REGION` | Yes | AWS region where the CloudWatch log groups and metrics are located |

### awslabs.eks-mcp-server

Provides tools for EKS cluster state inspection and pod-level log access. Used to check control plane health, addon status, and Kubernetes events during investigations.

**Tools provided:**

| Tool | Description |
|------|-------------|
| `get_cloudwatch_logs` | Retrieve CloudWatch logs for EKS components |
| `get_cloudwatch_metrics` | Retrieve CloudWatch metrics for EKS resources |
| `get_pod_logs` | Get logs from specific pods in the cluster |
| `get_k8s_events` | List Kubernetes events (warnings, errors) |
| `list_k8s_resources` | List Kubernetes resources (pods, nodes, deployments) |
| `get_eks_insights` | Get EKS cluster insights and health information |

**Environment variables:**

| Variable | Required | Description |
|----------|----------|-------------|
| `AWS_REGION` | Yes | AWS region where the EKS cluster is located |
| `EKS_CLUSTER_NAME` | Yes | Name of the EKS cluster to inspect |

## How It Works

Each MCP server runs as a subprocess launched by `uvx` (the `uv` tool runner). When Kiro starts, it reads `~/.kiro/settings/mcp.json` and starts the configured servers. The Kiro AI agent can then call the tools exposed by each server during scale test monitoring.

The servers are used in two modes:

1. **Proactive scanning** — During the scaling and hold-at-peak phases, the agent uses the Prometheus MCP server to scan AMP for fleet-wide anomalies (rising CPU/memory pressure, network errors, pod restart spikes).

2. **Reactive investigation** — When a rate drop alert is detected, the agent uses all three servers to correlate AMP metrics with CloudWatch Logs and EKS cluster state, building a root cause analysis.

## Graceful Degradation

You don't need all three servers configured. The scale test tool works with any combination:

- **No MCP servers** — The tool operates identically to the current behavior (K8s watch API, SSM diagnostics, event collection). The context file is still written for manual review.
- **Prometheus only** — Proactive AMP scanning works; reactive investigations use only AMP data.
- **Prometheus + CloudWatch** — Full metric and log correlation; no EKS cluster state inspection.
- **All three** — Full observability: metrics, logs, and cluster state correlation.
