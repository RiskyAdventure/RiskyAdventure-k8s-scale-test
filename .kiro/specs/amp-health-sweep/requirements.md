# Requirements Document

## Introduction

Replace the SSM-based node health sweep with a faster, fleet-wide approach using AMP (Amazon Managed Prometheus) PromQL queries and Kubernetes API node conditions. The current sweep SSMs into individual nodes during hold-at-peak, which is slow (~15s per node), limited to a sample of 10 nodes, requires SSM permissions on every node, and has a broken kubelet healthz check on EKS. The new implementation queries AMP for fleet-wide metrics and the K8s API for node conditions, covering all nodes in seconds rather than minutes. SSM is retained as an optional fallback for PSI counters when AMP is not configured.

## Glossary

- **Health_Sweep**: The module that samples fleet health during hold-at-peak, producing a results dict consumed by the summary, chart, and operator prompt pipeline.
- **AMP_Client**: A client that queries Amazon Managed Prometheus via the PromQL HTTP API using SigV4-signed requests.
- **K8s_Condition_Checker**: A component that reads node conditions (Ready, DiskPressure, MemoryPressure, PIDPressure) from the Kubernetes API.
- **SSM_Fallback**: The existing SSM-based sweep logic retained as an optional fallback for collecting low-level node data (PSI pressure, bpftrace probes, BPF tooling) not available through AMP.
- **Sweep_Result**: The output dict with keys `nodes_sampled`, `healthy`, `issues`, and `node_details`, consumed by downstream pipeline components.
- **TestConfig**: The dataclass holding test run configuration, including `amp_workspace_id` and `prometheus_url` fields.
- **PromQL**: The Prometheus query language used to retrieve time-series metrics from AMP.

## Requirements

### Requirement 1: AMP PromQL Metric Collection

**User Story:** As a scale test operator, I want fleet-wide node metrics collected via AMP PromQL queries, so that I can assess the health of all nodes in seconds without SSM access.

#### Acceptance Criteria

1. WHEN `amp_workspace_id` is configured in TestConfig, THE AMP_Client SHALL query AMP for CPU utilization per node using PromQL
2. WHEN `amp_workspace_id` is configured in TestConfig, THE AMP_Client SHALL query AMP for memory usage per node using PromQL
3. WHEN `amp_workspace_id` is configured in TestConfig, THE AMP_Client SHALL query AMP for disk usage per node using PromQL
4. WHEN `amp_workspace_id` is configured in TestConfig, THE AMP_Client SHALL query AMP for network error counts per node using PromQL
5. WHEN `amp_workspace_id` is configured in TestConfig, THE AMP_Client SHALL query AMP for pod restart counts per node using PromQL
6. WHEN `prometheus_url` is configured but `amp_workspace_id` is not, THE AMP_Client SHALL query the Prometheus URL directly without SigV4 signing
7. THE AMP_Client SHALL authenticate requests to AMP using SigV4 signing with the configured AWS credentials

### Requirement 2: Kubernetes API Node Condition Checks

**User Story:** As a scale test operator, I want node health assessed via Kubernetes API node conditions, so that I can detect kubelet and node-level issues across the entire fleet without relying on SSM or the kubelet healthz endpoint.

#### Acceptance Criteria

1. THE K8s_Condition_Checker SHALL query the Kubernetes API for node conditions on all nodes in the cluster
2. WHEN a node has a Ready condition with status not equal to "True", THE K8s_Condition_Checker SHALL report the node as unhealthy with the condition reason
3. WHEN a node has DiskPressure condition with status "True", THE K8s_Condition_Checker SHALL report a disk pressure issue for that node
4. WHEN a node has MemoryPressure condition with status "True", THE K8s_Condition_Checker SHALL report a memory pressure issue for that node
5. WHEN a node has PIDPressure condition with status "True", THE K8s_Condition_Checker SHALL report a PID pressure issue for that node

### Requirement 3: SSM Fallback for Low-Level Node Data

**User Story:** As a scale test operator, I want SSM-based node data collection retained as an optional fallback, so that I can collect low-level data not available through AMP such as PSI pressure counters, bpftrace probes, and BPF tooling output.

#### Acceptance Criteria

1. WHEN neither `amp_workspace_id` nor `prometheus_url` is configured in TestConfig, THE Health_Sweep SHALL use the SSM_Fallback as the primary collection method, running PSI pressure, kubelet status, disk usage, and per-core CPU checks on a sample of nodes
2. WHEN `amp_workspace_id` or `prometheus_url` is configured, THE Health_Sweep SHALL skip SSM-based collection for metrics available via AMP
3. THE SSM_Fallback SHALL accept an optional list of extra shell commands to run on sampled nodes, enabling operators to collect bpftrace output, BPF tool results, or other low-level diagnostics
4. THE SSM_Fallback SHALL reuse the existing `parse_sweep_output` function for parsing the standard health check command output
5. WHEN extra commands are provided, THE SSM_Fallback SHALL include their raw output in the per-node `node_details` under an `extra_diagnostics` key

### Requirement 4: Sweep Result Format Compatibility

**User Story:** As a scale test operator, I want the new health sweep to produce results in the same format as the current implementation, so that the summary, chart, and operator prompt pipeline continues to work without changes.

#### Acceptance Criteria

1. THE Health_Sweep SHALL return a Sweep_Result dict containing `nodes_sampled`, `healthy`, `issues`, and `node_details` keys
2. WHEN AMP metrics indicate a node exceeds a CPU utilization threshold, THE Health_Sweep SHALL add an issue string to the `issues` list identifying the node and metric value
3. WHEN AMP metrics indicate a node exceeds a memory usage threshold, THE Health_Sweep SHALL add an issue string to the `issues` list identifying the node and metric value
4. WHEN AMP metrics indicate a node exceeds a disk usage threshold, THE Health_Sweep SHALL add an issue string to the `issues` list identifying the node and metric value
5. WHEN AMP metrics indicate a node has network errors above a threshold, THE Health_Sweep SHALL add an issue string to the `issues` list identifying the node and error count
6. WHEN AMP metrics indicate a node has pod restarts above a threshold, THE Health_Sweep SHALL add an issue string to the `issues` list identifying the node and restart count
7. WHEN K8s node conditions indicate an unhealthy node, THE Health_Sweep SHALL add an issue string to the `issues` list identifying the node and condition
8. THE Health_Sweep SHALL populate `node_details` with per-node entries containing node name, status, and issues for each node assessed
9. THE Health_Sweep SHALL set `nodes_sampled` to the total number of nodes assessed (all nodes when using AMP/K8s, sample size when using SSM)
10. THE Health_Sweep SHALL set `healthy` to the count of nodes with zero issues

### Requirement 5: Performance

**User Story:** As a scale test operator, I want the health sweep to complete in seconds, so that it does not delay the hold-at-peak phase.

#### Acceptance Criteria

1. WHEN using AMP and K8s API collection, THE Health_Sweep SHALL complete all queries and produce results within 30 seconds for a fleet of up to 200 nodes
2. THE AMP_Client SHALL execute PromQL queries concurrently using asyncio to minimize wall-clock time
3. THE K8s_Condition_Checker SHALL retrieve all node conditions in a single Kubernetes API call

### Requirement 6: Error Handling

**User Story:** As a scale test operator, I want the health sweep to handle query failures gracefully, so that a failed AMP query does not crash the test run.

#### Acceptance Criteria

1. IF an AMP PromQL query fails, THEN THE AMP_Client SHALL log the error and return an empty result for that metric category
2. IF the Kubernetes API node condition query fails, THEN THE K8s_Condition_Checker SHALL log the error and return an empty list of conditions
3. IF all AMP queries fail, THEN THE Health_Sweep SHALL return a Sweep_Result with `nodes_sampled` set to 0 and an issue string describing the failure
4. THE Health_Sweep SHALL persist the Sweep_Result to the evidence store regardless of partial failures

### Requirement 7: Evidence Persistence

**User Story:** As a scale test operator, I want sweep results persisted to the evidence store, so that I can review fleet health data after the test run.

#### Acceptance Criteria

1. THE Health_Sweep SHALL write the Sweep_Result as JSON to `diagnostics/health_sweep.json` in the run directory
2. WHEN AMP metrics are collected, THE Health_Sweep SHALL include the raw PromQL response data in the persisted result under a `raw_metrics` key
