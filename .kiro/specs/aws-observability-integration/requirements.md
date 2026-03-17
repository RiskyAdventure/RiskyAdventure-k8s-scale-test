# Requirements Document

## Introduction

The k8s scale test tool currently detects anomalies reactively: when the pod ready rate drops, the `AnomalyDetector` collects evidence from K8s events, pod state, node conditions, EC2 ENI state, and SSM node-level commands (hardcoded `journalctl`, `top`, `df` on up to 3 nodes). This approach has three gaps: (1) it only investigates after a rate drop is already happening, (2) SSM commands are slow and limited to a handful of nodes, and (3) once the test ends and pods scale down, the observability signal disappears.

This feature replaces the static, reactive diagnostics with an AI sub-agent that runs inside Kiro's agent loop and uses three AWS MCP servers as tools. The sub-agent proactively scans Amazon Managed Prometheus (AMP) for emerging fleet-wide issues during the test, dynamically investigates anomalies by correlating AMP metrics with CloudWatch Logs and EKS cluster state, and persists structured findings to the evidence store. Instead of hardcoded PromQL queries and thresholds, the agent reasons about what to look for and what to investigate next.

## Glossary

- **Scale_Test_Tool**: The Python-based EKS scale testing system at `src/k8s_scale_test/` that orchestrates pod scaling, monitors ready rates, and detects anomalies.
- **Kiro_Agent**: The AI agent running inside Kiro's agent loop that has native access to MCP tools. The sub-agent is invoked via Kiro hook files with `askAgent` action type.
- **MCP_Server**: A Model Context Protocol server that exposes a set of tools the Kiro_Agent can call. Each MCP server wraps a specific AWS service API.
- **Prometheus_MCP_Server**: The `awslabs.prometheus-mcp-server` MCP server providing tools to query AMP via PromQL (`ExecuteQuery`, `ExecuteRangeQuery`, `ListMetrics`, `GetAvailableWorkspaces`, `GetServerInfo`).
- **CloudWatch_MCP_Server**: The `awslabs.cloudwatch-mcp-server` MCP server providing tools for CloudWatch Logs analysis (`execute_log_insights_query`, `get_logs_insight_query_results`, `analyze_log_group`, `get_metric_data`, `get_active_alarms`, `cancel_logs_insight_query`).
- **EKS_MCP_Server**: The `awslabs.eks-mcp-server` MCP server providing tools for EKS cluster state and logs (`get_cloudwatch_logs`, `get_cloudwatch_metrics`, `get_pod_logs`, `get_k8s_events`, `list_k8s_resources`, `get_eks_insights`).
- **Context_File**: A JSON file written by the Python tool during the test lifecycle containing current test state (phase, timestamps, node lists, namespaces, alerts, AMP workspace ID, CloudWatch log groups, EKS cluster name) that the Kiro_Agent reads to understand what to investigate.
- **Agent_Finding**: A structured JSON file written by the Kiro_Agent to the evidence store containing the agent's observations, correlations, and recommendations.
- **Evidence_Store**: The existing persistence layer (`evidence.py`) that saves all test artifacts to disk in JSON format, serving as the shared interface between the Python tool and the Kiro_Agent.
- **Steering_File**: A Kiro steering file (`.kiro/steering/*.md`) that provides persistent instructions to the Kiro_Agent about how to approach observability analysis during scale tests.
- **Hook_File**: A Kiro hook file (`.kiro/hooks/*.md`) that defines triggers and actions. Hooks with `askAgent` action type invoke the Kiro_Agent with a prompt.
- **Controller**: The test orchestrator (`controller.py`) that manages the full test lifecycle from preflight through scaling, hold-at-peak, and cleanup.
- **Monitor**: The pod rate monitor (`monitor.py`) that tracks pod ready rates via K8s watch API on a 5-second tick.
- **Anomaly_Detector**: The existing anomaly detection system (`anomaly.py`) that collects evidence layers and correlates findings.
- **Finding**: A structured evidence record produced by the Anomaly_Detector containing symptom, root cause, severity, and evidence references.

## Requirements

### Requirement 1: MCP Server Configuration

**User Story:** As a scale test operator, I want the three AWS MCP servers (Prometheus, CloudWatch, EKS) configured in Kiro's MCP settings, so that the AI sub-agent has access to fleet-wide observability tools during scale tests.

#### Acceptance Criteria

1. THE Scale_Test_Tool documentation SHALL include the MCP server configuration entries for `awslabs.prometheus-mcp-server`, `awslabs.cloudwatch-mcp-server`, and `awslabs.eks-mcp-server` to be added to `~/.kiro/settings/mcp.json`.
2. WHEN the Prometheus_MCP_Server is configured, THE Kiro_Agent SHALL have access to the `ExecuteQuery`, `ExecuteRangeQuery`, `ListMetrics`, `GetAvailableWorkspaces`, and `GetServerInfo` tools.
3. WHEN the CloudWatch_MCP_Server is configured, THE Kiro_Agent SHALL have access to the `execute_log_insights_query`, `get_logs_insight_query_results`, `analyze_log_group`, `get_metric_data`, `get_active_alarms`, and `cancel_logs_insight_query` tools.
4. WHEN the EKS_MCP_Server is configured, THE Kiro_Agent SHALL have access to the `get_cloudwatch_logs`, `get_cloudwatch_metrics`, `get_pod_logs`, `get_k8s_events`, `list_k8s_resources`, and `get_eks_insights` tools.

### Requirement 2: Test Context File Generation

**User Story:** As a scale test operator, I want the Python tool to write test context files during the test lifecycle, so that the AI sub-agent knows what cluster, namespaces, time windows, and alerts to investigate.

#### Acceptance Criteria

1. WHEN the Controller enters the scaling phase, THE Scale_Test_Tool SHALL write a context file to the evidence store containing: run ID, test start timestamp, target pod count, namespace list, AMP workspace ID, CloudWatch log group names, EKS cluster name, and current test phase.
2. WHEN the test phase changes (scaling, hold-at-peak, cleanup), THE Scale_Test_Tool SHALL update the context file with the new phase and its start timestamp.
3. WHEN the Monitor detects a rate drop alert, THE Scale_Test_Tool SHALL append the alert details (timestamp, current rate, rolling average, ready count, pending count) to an alerts section in the context file.
4. WHEN the Anomaly_Detector produces a Finding, THE Scale_Test_Tool SHALL append a summary of the finding (finding ID, severity, symptom, affected resources) to the context file.
5. THE context file SHALL be written as `agent_context.json` in the run directory of the Evidence_Store.
6. WHEN the AMP workspace ID, CloudWatch log group, or EKS cluster name is not configured, THE context file SHALL omit the corresponding field rather than including a null value.

### Requirement 3: Kiro Agent Hooks for Proactive Monitoring

**User Story:** As a scale test operator, I want Kiro agent hooks that trigger the AI sub-agent to proactively scan AMP metrics during the scaling and hold-at-peak phases, so that emerging issues are caught before they cause rate drops.

#### Acceptance Criteria

1. THE Scale_Test_Tool SHALL include a Kiro hook file that triggers a proactive AMP scan when the context file indicates the test is in the scaling or hold-at-peak phase.
2. WHEN the proactive scan hook triggers, THE hook SHALL use the `askAgent` action type with a prompt instructing the Kiro_Agent to read the context file, query AMP for fleet-wide metrics using the Prometheus_MCP_Server tools, identify anomalous patterns, and write findings to the evidence store.
3. THE proactive scan prompt SHALL instruct the Kiro_Agent to look for emerging issues (rising CPU/memory pressure, network errors, disk IO saturation, pod restart spikes) without specifying hardcoded PromQL queries or thresholds.
4. THE proactive scan prompt SHALL instruct the Kiro_Agent to write each finding as a separate JSON file in the `findings/` subdirectory of the run directory, following the Agent_Finding schema.

### Requirement 4: Kiro Agent Hooks for Reactive Investigation

**User Story:** As a scale test operator, I want a Kiro agent hook that triggers the AI sub-agent to investigate when anomalies are detected, so that the agent can correlate AMP metrics with CloudWatch Logs and EKS state to identify root causes.

#### Acceptance Criteria

1. THE Scale_Test_Tool SHALL include a Kiro hook file that triggers a reactive investigation when the context file contains new alert entries.
2. WHEN the reactive investigation hook triggers, THE hook SHALL use the `askAgent` action type with a prompt instructing the Kiro_Agent to read the alert details from the context file, investigate using all three MCP servers, correlate findings, and write a detailed investigation report to the evidence store.
3. THE reactive investigation prompt SHALL instruct the Kiro_Agent to: (a) query AMP for resource metrics on affected nodes and time windows, (b) query CloudWatch Logs for error patterns correlated with the alert timestamp, (c) check EKS cluster state for control plane issues or addon health problems, and (d) synthesize a root cause analysis.
4. THE reactive investigation prompt SHALL instruct the Kiro_Agent to reason dynamically about what to investigate next based on initial findings, rather than following a fixed sequence of queries.
5. THE reactive investigation prompt SHALL instruct the Kiro_Agent to write the investigation report as a JSON file in the `findings/` subdirectory, following the Agent_Finding schema.

### Requirement 5: Skeptical Review Agent

**User Story:** As a scale test operator, I want a skeptical review agent that independently verifies investigation findings before they reach me, so that I can trust the analysis confidence level and catch runaway reasoning where an early mistake propagates through subsequent steps.

#### Acceptance Criteria

1. THE Scale_Test_Tool SHALL include a Kiro hook file that triggers a skeptical review after the investigation agent writes a finding to the evidence store.
2. WHEN the review hook triggers, THE hook SHALL use the `askAgent` action type with a prompt instructing the Kiro_Agent to: (a) read the investigation finding, (b) independently verify key claims by re-querying the MCP servers, (c) check the source code and documentation for context, and (d) annotate the finding with a confidence assessment.
3. THE review agent SHALL assign a confidence level (high, medium, low) to each finding, with reasoning explaining why the confidence is at that level.
4. THE review agent SHALL list alternative explanations that the investigation agent may have missed, based on independent analysis of the same data.
5. WHEN the review agent's confidence level is below "high", THE review agent SHALL write checkpoint questions to the finding asking the operator whether the investigation direction is correct or if additional context should be considered.
6. THE review agent SHALL write its assessment as a `review` field appended to the original finding JSON, containing: confidence level, reasoning, alternative explanations, and checkpoint questions.
7. THE review agent SHALL independently query MCP servers to verify at least one key claim from the investigation finding, rather than accepting the investigation agent's evidence at face value.

### Requirement 6: Agent Finding Schema and Evidence Store Integration

**User Story:** As a scale test operator, I want agent findings persisted in a structured JSON format that the Python tool can read, so that agent observations are included in the post-run report and chart.

#### Acceptance Criteria

1. THE Agent_Finding JSON schema SHALL include: finding ID, timestamp, source (proactive-scan, reactive-investigation, or skeptical-review), severity (info, warning, critical), title, description, affected resources list, evidence references list, MCP queries executed list, recommended actions list, and an optional review field.
2. THE review field (when present) SHALL include: confidence level (high, medium, low), reasoning string, alternative explanations list, checkpoint questions list, and verification results list.
3. WHEN the Kiro_Agent writes a finding, THE finding SHALL be written as a JSON file named `agent-{finding_id}.json` in the `findings/` subdirectory of the current run directory.
4. WHEN the Controller generates the post-run summary, THE Scale_Test_Tool SHALL read all `agent-*.json` files from the findings directory and include them in the test run summary.
5. THE Evidence_Store SHALL provide a method to load all agent findings for a given run ID, returning them as a list of parsed dictionaries.
6. IF the findings directory contains malformed agent finding JSON files, THEN THE Evidence_Store SHALL log a warning and skip the malformed file without failing the summary generation.

### Requirement 7: Steering File for Agent Observability Guidance

**User Story:** As a scale test operator, I want a steering file that guides the AI sub-agent on how to approach observability analysis during EKS scale tests, so that the agent produces relevant and actionable findings without hardcoded queries.

#### Acceptance Criteria

1. THE Scale_Test_Tool SHALL include a Kiro steering file at `.kiro/steering/scale-test-observability.md` that provides the Kiro_Agent with domain knowledge about EKS scale test observability.
2. THE steering file SHALL describe the types of issues to look for in AMP metrics (CPU/memory pressure, network errors, VPC CNI failures, disk IO saturation, Karpenter provisioning delays) without specifying exact PromQL queries.
3. THE steering file SHALL describe how to correlate AMP metric anomalies with CloudWatch Logs (matching time windows, filtering by affected node hostnames, looking for IPAMD errors, OOM kills, kubelet failures).
4. THE steering file SHALL describe how to use EKS MCP tools to check cluster-level context (control plane health, addon status, pod logs for system components).
5. THE steering file SHALL describe the Agent_Finding JSON schema (including the review field) so the agent writes correctly structured findings.
6. THE steering file SHALL instruct the agent to prioritize issues that could cause pod ready rate drops and to capture evidence while the test is still running (before pods scale down and signal disappears).
7. THE steering file SHALL describe the skeptical review process: how to independently verify claims, assign confidence levels, identify alternative explanations, and write checkpoint questions.

### Requirement 8: Python Tool Configuration for Agent Integration

**User Story:** As a scale test operator, I want to configure AMP workspace ID, CloudWatch log groups, and EKS cluster name via CLI arguments, so that the context file contains the information the agent needs to query the right resources.

#### Acceptance Criteria

1. THE TestConfig SHALL include optional fields for AMP workspace ID, CloudWatch log group name, and EKS cluster name.
2. WHEN the operator provides `--amp-workspace-id` on the CLI, THE Scale_Test_Tool SHALL include the AMP workspace ID in the agent context file.
3. WHEN the operator provides `--cloudwatch-log-group` on the CLI, THE Scale_Test_Tool SHALL include the CloudWatch log group name in the agent context file.
4. WHEN the operator provides `--eks-cluster-name` on the CLI, THE Scale_Test_Tool SHALL include the EKS cluster name in the agent context file.
5. WHEN no observability CLI arguments are provided, THE Scale_Test_Tool SHALL omit the corresponding fields from the context file and the Kiro_Agent SHALL operate with whichever MCP tools are available.

### Requirement 9: Post-Run Report Integration with Agent Findings

**User Story:** As a scale test operator, I want agent findings included in the post-run report and HTML chart, so that I can review AI-driven observations alongside the existing test results.

#### Acceptance Criteria

1. WHEN agent findings exist in the run directory, THE Scale_Test_Tool SHALL include an agent findings section in the TestRunSummary containing the count of findings by severity and a list of finding summaries.
2. WHEN agent findings exist in the run directory, THE chart generator SHALL include an agent findings timeline showing when each finding was produced relative to the test timeline.
3. WHEN no agent findings exist in the run directory, THE Scale_Test_Tool SHALL produce the report and chart with existing data and omit the agent findings sections.

### Requirement 10: Performance Safety

**User Story:** As a scale test operator, I want the agent integration to never block the monitor ticker or the scaling loop, so that test accuracy is preserved.

#### Acceptance Criteria

1. THE context file writes SHALL be non-blocking: the Controller SHALL write the context file using synchronous file I/O that completes in under 10 milliseconds (single JSON write to local disk).
2. THE Monitor ticker loop (5-second interval) SHALL continue uninterrupted regardless of whether the Kiro_Agent is running, since the agent runs in a separate Kiro agent loop process.
3. WHEN the Kiro_Agent is unavailable or MCP servers are not configured, THE Scale_Test_Tool SHALL continue operating with existing monitoring capabilities and the context file SHALL still be written for potential manual review.

### Requirement 11: Graceful Degradation

**User Story:** As a scale test operator, I want the tool to work correctly even when MCP servers are unavailable or observability services are not configured, so that the core scale test functionality is never compromised.

#### Acceptance Criteria

1. WHEN the Prometheus_MCP_Server is not configured in Kiro's MCP settings, THE Scale_Test_Tool SHALL operate normally and the proactive scan hook SHALL produce no findings.
2. WHEN the CloudWatch_MCP_Server is not configured, THE reactive investigation hook SHALL investigate using only the available MCP servers.
3. WHEN the EKS_MCP_Server is not configured, THE reactive investigation hook SHALL investigate using only the available MCP servers.
4. WHEN no MCP servers are configured, THE Scale_Test_Tool SHALL operate identically to the current behavior (K8s watch, SSM diagnostics, event collection).
5. IF the Kiro_Agent writes a finding with an unexpected schema, THEN THE Scale_Test_Tool SHALL log a warning and skip the malformed finding without affecting the test run.
