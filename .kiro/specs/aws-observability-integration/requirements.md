# Requirements Document

## Introduction

The k8s scale test tool currently collects evidence from K8s APIs (watch, events, metrics-server), SSM node-level commands, and EC2 ENI state. While effective for reactive diagnostics, the tool lacks continuous time-series metrics (CPU/memory/network/disk across the entire fleet), centralized log analysis (kubelet/containerd/IPAMD logs without per-node SSM), and cluster-level EKS insights (control plane metrics, addon health). This feature integrates AWS observability services — Amazon Managed Prometheus (AMP), CloudWatch Logs, and EKS API — via dedicated client modules to provide richer evidence during and after scale tests. Analysis agents consume this data to detect issues the current 6-layer anomaly detector cannot see: fleet-wide resource trends, log pattern correlation across hundreds of nodes, and control plane throttling.

## Glossary

- **Scale_Test_Tool**: The Python-based EKS scale testing system at `src/k8s_scale_test/` that orchestrates pod scaling, monitors ready rates, and detects anomalies.
- **AMP_Client**: A client module that queries Amazon Managed Prometheus (AMP) via PromQL for time-series metrics collected by Prometheus agents running on the EKS cluster.
- **CloudWatch_Logs_Client**: A client module that queries CloudWatch Logs Insights for centralized kubelet, containerd, and VPC CNI (IPAMD) logs shipped from cluster nodes.
- **EKS_API_Client**: A client module that queries the EKS DescribeCluster and DescribeAddonVersions APIs for cluster-level health, addon status, and control plane metadata.
- **Metrics_Analyzer_Agent**: An analysis agent that queries AMP for fleet-wide resource metrics and produces a time-series summary identifying nodes or time windows with anomalous resource usage during a test run.
- **Log_Analyzer_Agent**: An analysis agent that queries CloudWatch Logs Insights for error patterns across the fleet and produces a structured summary of log-based issues correlated with test timeline.
- **Post_Run_Reporter**: An agent that runs after test completion to produce a comprehensive observability report combining AMP metrics, CloudWatch log analysis, and EKS cluster state.
- **Evidence_Store**: The existing persistence layer (`evidence.py`) that saves all test artifacts to disk in JSON format.
- **Anomaly_Detector**: The existing anomaly detection system (`anomaly.py`) that collects 6 layers of evidence and correlates findings.
- **Controller**: The test orchestrator (`controller.py`) that manages the full test lifecycle from preflight through scaling, hold-at-peak, and cleanup.
- **Monitor**: The pod rate monitor (`monitor.py`) that tracks pod ready rates via K8s watch API on a 5-second tick.
- **Finding**: A structured evidence record produced by the Anomaly_Detector containing symptom, root cause, severity, and evidence references.

## Requirements

### Requirement 1: AMP Client for Fleet-Wide Metrics

**User Story:** As a scale test operator, I want to query AMP for fleet-wide resource metrics during and after tests, so that I can see CPU, memory, network, and disk utilization trends across all nodes without per-node SSM calls.

#### Acceptance Criteria

1. WHEN the Scale_Test_Tool is configured with an AMP workspace endpoint, THE AMP_Client SHALL execute PromQL range queries against the AMP workspace and return structured time-series results.
2. WHEN the AMP_Client executes a PromQL query, THE AMP_Client SHALL accept a time range (start, end) and step interval, and return a list of time-series data points with metric name, labels, and values.
3. WHEN the AMP_Client receives an HTTP error or timeout from the AMP endpoint, THE AMP_Client SHALL return an error result with the HTTP status code and message without raising an exception to the caller.
4. WHEN the AMP_Client is asked for node resource metrics, THE AMP_Client SHALL provide pre-built PromQL queries for CPU utilization, memory utilization, network bytes transmitted and received, and disk IO utilization per node.
5. WHEN the AMP_Client is asked for pod-level metrics, THE AMP_Client SHALL provide pre-built PromQL queries for container CPU usage, memory working set, and restart counts aggregated by namespace.
6. THE AMP_Client SHALL use AWS SigV4 authentication for all requests to the AMP endpoint.
7. WHEN the AMP workspace endpoint is not configured, THE AMP_Client SHALL report that AMP is unavailable and all query methods SHALL return empty results.

### Requirement 2: CloudWatch Logs Client for Centralized Log Analysis

**User Story:** As a scale test operator, I want to query CloudWatch Logs for kubelet, containerd, and IPAMD log patterns across the fleet, so that I can identify error patterns without running SSM commands on individual nodes.

#### Acceptance Criteria

1. WHEN the Scale_Test_Tool is configured with a CloudWatch log group name, THE CloudWatch_Logs_Client SHALL execute CloudWatch Logs Insights queries against the specified log group and return structured results.
2. WHEN the CloudWatch_Logs_Client executes a query, THE CloudWatch_Logs_Client SHALL accept a time range (start, end) and a Logs Insights query string, start the query asynchronously, poll for completion, and return the results.
3. WHEN the CloudWatch_Logs_Client query exceeds a configurable timeout (default 30 seconds), THE CloudWatch_Logs_Client SHALL cancel the running query and return a timeout error result.
4. WHEN the CloudWatch_Logs_Client is asked for error patterns, THE CloudWatch_Logs_Client SHALL provide pre-built Logs Insights queries for kubelet errors, containerd errors, IPAMD datastore exhaustion messages, and OOM kill events.
5. WHEN the CloudWatch_Logs_Client is asked for node-specific logs, THE CloudWatch_Logs_Client SHALL filter results by hostname or instance ID.
6. WHEN the CloudWatch log group is not configured, THE CloudWatch_Logs_Client SHALL report that CloudWatch Logs is unavailable and all query methods SHALL return empty results.
7. WHEN the CloudWatch_Logs_Client polls for query results, THE CloudWatch_Logs_Client SHALL use exponential backoff starting at 0.5 seconds with a maximum interval of 5 seconds.

### Requirement 3: EKS API Client for Cluster-Level Insights

**User Story:** As a scale test operator, I want to query the EKS API for cluster health and addon status, so that I can detect control plane issues and addon version mismatches that affect scaling.

#### Acceptance Criteria

1. WHEN the Scale_Test_Tool is configured with a cluster name, THE EKS_API_Client SHALL query the EKS DescribeCluster API and return cluster status, Kubernetes version, platform version, and endpoint information.
2. WHEN the EKS_API_Client queries addon status, THE EKS_API_Client SHALL return the name, version, status, and health issues for each installed addon (vpc-cni, kube-proxy, coredns, ebs-csi-driver).
3. IF an EKS API call fails, THEN THE EKS_API_Client SHALL return an error result with the exception message without raising an exception to the caller.
4. WHEN the cluster name is not configured, THE EKS_API_Client SHALL report that EKS API is unavailable and all query methods SHALL return empty results.

### Requirement 4: Metrics Analyzer Agent

**User Story:** As a scale test operator, I want an agent that analyzes AMP metrics during or after a test run, so that I can identify fleet-wide resource bottlenecks that the current per-node SSM diagnostics miss.

#### Acceptance Criteria

1. WHEN the Metrics_Analyzer_Agent runs for a test time window, THE Metrics_Analyzer_Agent SHALL query AMP for CPU, memory, network, and disk metrics across all nodes and produce a structured summary.
2. WHEN the Metrics_Analyzer_Agent produces a summary, THE summary SHALL include: nodes with CPU utilization above 80%, nodes with memory utilization above 85%, nodes with network throughput anomalies, and the fleet-wide average for each metric.
3. WHEN the Metrics_Analyzer_Agent detects nodes exceeding resource thresholds, THE Metrics_Analyzer_Agent SHALL rank the nodes by severity and include the top 10 in the summary.
4. WHEN AMP is unavailable, THE Metrics_Analyzer_Agent SHALL return an empty summary with a status indicating AMP was not configured.
5. THE Metrics_Analyzer_Agent SHALL complete analysis within 30 seconds for a fleet of up to 300 nodes.
6. WHEN the Metrics_Analyzer_Agent produces a summary, THE Evidence_Store SHALL persist the summary as a JSON file in the run directory.

### Requirement 5: Log Analyzer Agent

**User Story:** As a scale test operator, I want an agent that analyzes CloudWatch Logs during or after a test run, so that I can see fleet-wide error patterns correlated with the test timeline.

#### Acceptance Criteria

1. WHEN the Log_Analyzer_Agent runs for a test time window, THE Log_Analyzer_Agent SHALL query CloudWatch Logs for error patterns and produce a structured summary.
2. WHEN the Log_Analyzer_Agent produces a summary, THE summary SHALL include: error counts grouped by log source (kubelet, containerd, IPAMD), the top error messages by frequency, and the time distribution of errors relative to the test timeline.
3. WHEN the Log_Analyzer_Agent detects IPAMD datastore exhaustion messages, THE Log_Analyzer_Agent SHALL flag the affected nodes and include the count in the summary.
4. WHEN CloudWatch Logs is unavailable, THE Log_Analyzer_Agent SHALL return an empty summary with a status indicating CloudWatch Logs was not configured.
5. WHEN the Log_Analyzer_Agent produces a summary, THE Evidence_Store SHALL persist the summary as a JSON file in the run directory.

### Requirement 6: Post-Run Observability Report

**User Story:** As a scale test operator, I want a comprehensive observability report generated after each test run, so that I can review fleet-wide metrics, log patterns, and cluster state alongside the existing test results.

#### Acceptance Criteria

1. WHEN a test run completes, THE Post_Run_Reporter SHALL collect AMP metrics summary, CloudWatch Logs summary, and EKS cluster state for the test time window.
2. WHEN the Post_Run_Reporter produces a report, THE report SHALL include sections for fleet resource utilization, log error analysis, cluster and addon health, and a correlation of observability findings with existing anomaly detector findings.
3. WHEN any observability source is unavailable, THE Post_Run_Reporter SHALL include a note indicating which sources were unavailable and produce the report with available data.
4. WHEN the Post_Run_Reporter produces a report, THE Evidence_Store SHALL persist the report as a JSON file in the run directory.
5. THE Post_Run_Reporter SHALL complete report generation within 60 seconds.

### Requirement 7: Anomaly Detector Integration

**User Story:** As a scale test operator, I want the anomaly detector to incorporate AMP metrics and CloudWatch Logs evidence when investigating rate drops, so that findings include fleet-wide context beyond per-node SSM data.

#### Acceptance Criteria

1. WHEN the Anomaly_Detector investigates a rate drop alert and AMP is configured, THE Anomaly_Detector SHALL query AMP for resource metrics on nodes with stuck pods and include the results in the Finding evidence references.
2. WHEN the Anomaly_Detector investigates a rate drop alert and CloudWatch Logs is configured, THE Anomaly_Detector SHALL query CloudWatch Logs for recent errors on nodes with stuck pods and include the results in the Finding evidence references.
3. WHEN AMP or CloudWatch Logs queries fail during anomaly investigation, THE Anomaly_Detector SHALL log the failure and continue with remaining evidence layers without blocking the investigation.
4. WHEN AMP or CloudWatch Logs are not configured, THE Anomaly_Detector SHALL skip those evidence layers and produce findings using existing evidence sources.

### Requirement 8: Controller Lifecycle Integration

**User Story:** As a scale test operator, I want the observability components to start and stop with the test lifecycle, so that data collection covers the full test window without manual intervention.

#### Acceptance Criteria

1. WHEN the Controller initializes monitoring components, THE Controller SHALL create AMP_Client, CloudWatch_Logs_Client, and EKS_API_Client instances based on the test configuration.
2. WHEN the Controller reaches the hold-at-peak phase, THE Controller SHALL run the Metrics_Analyzer_Agent and Log_Analyzer_Agent concurrently with the existing health sweep.
3. WHEN the Controller completes the test run (after cleanup), THE Controller SHALL run the Post_Run_Reporter to generate the observability report.
4. THE Controller SHALL pass the observability clients to the Anomaly_Detector so that AMP and CloudWatch evidence layers are available during scaling.
5. WHEN observability clients fail to initialize (missing configuration or credentials), THE Controller SHALL log a warning and proceed with the test using existing monitoring capabilities.

### Requirement 9: Configuration

**User Story:** As a scale test operator, I want to configure AWS observability endpoints via CLI arguments and TestConfig, so that I can enable or disable each observability source per test run.

#### Acceptance Criteria

1. THE TestConfig SHALL include optional fields for AMP workspace endpoint URL, CloudWatch log group name, and EKS cluster name.
2. WHEN the operator provides `--amp-endpoint` on the CLI, THE Scale_Test_Tool SHALL configure the AMP_Client with the specified endpoint.
3. WHEN the operator provides `--cloudwatch-log-group` on the CLI, THE Scale_Test_Tool SHALL configure the CloudWatch_Logs_Client with the specified log group.
4. WHEN the operator provides `--eks-cluster-name` on the CLI, THE Scale_Test_Tool SHALL configure the EKS_API_Client with the specified cluster name.
5. WHEN no observability CLI arguments are provided, THE Scale_Test_Tool SHALL operate with existing monitoring capabilities and all observability clients SHALL report unavailable status.

### Requirement 10: Performance Safety

**User Story:** As a scale test operator, I want observability queries to never block the monitor ticker or the scaling loop, so that test accuracy is preserved.

#### Acceptance Criteria

1. THE AMP_Client, CloudWatch_Logs_Client, and EKS_API_Client SHALL execute all network calls in thread executors or background tasks so that the Monitor ticker loop is never blocked.
2. WHEN the Anomaly_Detector queries observability clients during an investigation, THE queries SHALL run concurrently with existing evidence collection using asyncio.gather.
3. WHEN any observability query exceeds its timeout, THE query SHALL be cancelled and the calling agent SHALL proceed with available data.
4. THE Metrics_Analyzer_Agent and Log_Analyzer_Agent SHALL run as asyncio tasks during hold-at-peak, concurrent with the existing HealthSweepAgent.

### Requirement 11: Chart Enhancement with AMP Metrics

**User Story:** As a scale test operator, I want the HTML chart to include fleet-wide resource utilization data from AMP alongside the existing rate and count charts, so that I can visually correlate resource usage with scaling behavior.

#### Acceptance Criteria

1. WHEN AMP metrics summary data exists in the run directory, THE chart generator SHALL include a fleet resource utilization section showing average CPU and memory utilization over time.
2. WHEN AMP metrics summary data is not present, THE chart generator SHALL produce the chart with existing data and omit the resource utilization section.
3. WHEN the chart includes AMP data, THE chart SHALL display the data as additional Chart.js time-series plots aligned to the same time axis as the existing rate and count charts.
