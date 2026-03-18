# Requirements Document

## Introduction

Add AMP/Prometheus metric querying to the AnomalyDetector's reactive investigation pipeline. Currently, when a rate drop or timeout triggers an investigation, the AnomalyDetector collects K8s events, pod phases, stuck nodes, EC2 ENI state, and SSM diagnostics — but never queries AMP for node-level metrics around the alert timestamp. The ObservabilityScanner queries AMP proactively but its findings are not shared with the anomaly detector. This feature bridges that gap by adding an AMP investigation step to `handle_alert()`, querying node-level metrics (CPU/memory pressure, network errors, IPAMD-related metrics, pod restart counts) for affected nodes within a ±3 minute window around the alert.

## Glossary

- **AnomalyDetector**: The reactive investigation module (`anomaly.py`) that runs a multi-layer evidence collection pipeline when the monitor fires an alert.
- **AMPMetricCollector**: The existing class in `health_sweep.py` that executes PromQL queries against AMP with SigV4 signing and credential refresh.
- **Finding**: The data model (`models.py`) representing an investigation result, containing severity, root cause, evidence references, K8s events, node metrics, and node diagnostics.
- **Alert**: The data model representing a rate drop, pending timeout, or monitor gap event that triggers investigation.
- **AMP**: Amazon Managed Service for Prometheus — the hosted Prometheus endpoint used for fleet-wide metric queries.
- **Investigation_Pipeline**: The ordered sequence of evidence collection layers in `handle_alert()`: K8s events → KB lookup → pod phases → stuck nodes → node conditions → ENI state → SSM diagnostics → correlation.
- **Evidence_References**: A list of human-readable summary strings in a Finding that describe what evidence was collected and what it showed.

## Requirements

### Requirement 1: Pass AMP Configuration to AnomalyDetector

**User Story:** As a scale test operator, I want the AnomalyDetector to have access to AMP configuration, so that it can query Prometheus metrics during reactive investigations.

#### Acceptance Criteria

1. WHEN the AnomalyDetector is constructed with an AMPMetricCollector instance, THE AnomalyDetector SHALL store the collector for use during investigations.
2. WHEN the AnomalyDetector is constructed without an AMPMetricCollector instance, THE AnomalyDetector SHALL operate without AMP queries and skip the AMP investigation layer.
3. THE AnomalyDetector constructor SHALL accept an optional `amp_collector` parameter of type AMPMetricCollector.

### Requirement 2: Query AMP Metrics During Investigation

**User Story:** As a scale test operator, I want the AnomalyDetector to query AMP for node-level metrics around the alert timestamp, so that I get Prometheus-based evidence alongside K8s events and SSM diagnostics.

#### Acceptance Criteria

1. WHEN an alert triggers investigation and an AMPMetricCollector is configured, THE AnomalyDetector SHALL query AMP for node-level metrics after the K8s event collection layer and before the correlation step.
2. WHEN querying AMP, THE AnomalyDetector SHALL request metrics for the following categories: CPU utilization, memory utilization, network error rates, IPAMD-related metrics, and pod restart counts.
3. WHEN querying AMP, THE AnomalyDetector SHALL scope queries to a ±3 minute window around the alert timestamp.
4. THE AnomalyDetector SHALL run all AMP metric queries concurrently using the AMPMetricCollector.

### Requirement 3: Integrate AMP Evidence into Findings

**User Story:** As a scale test operator, I want AMP metric results included in the investigation Finding, so that I can see Prometheus-based evidence alongside other investigation layers.

#### Acceptance Criteria

1. WHEN AMP metrics are collected, THE AnomalyDetector SHALL add AMP metric summaries to the Finding's evidence_references list.
2. WHEN AMP metrics reveal threshold violations (CPU > 90%, memory > 90%, network errors > 0, pod restarts > 5), THE AnomalyDetector SHALL include the violations as evidence references with node names and values.
3. WHEN AMP metrics are collected, THE AnomalyDetector SHALL pass the AMP metric results to the correlation step for root cause extraction.
4. WHEN AMP metrics show node-level anomalies, THE _extract_root_cause method SHALL incorporate AMP evidence into the root cause string.

### Requirement 4: Graceful Degradation

**User Story:** As a scale test operator, I want AMP queries to degrade gracefully, so that investigation pipeline failures in the AMP layer do not block the rest of the investigation.

#### Acceptance Criteria

1. IF an AMP query fails with a network or authentication error, THEN THE AnomalyDetector SHALL log the error and continue the investigation pipeline without AMP evidence.
2. IF the AMPMetricCollector is not configured (None), THEN THE AnomalyDetector SHALL skip the AMP investigation layer entirely and proceed to the next layer.
3. IF all AMP queries return empty results, THEN THE AnomalyDetector SHALL add an evidence reference noting that AMP queries returned no data and continue the investigation.

### Requirement 5: Controller Wiring

**User Story:** As a scale test operator, I want the controller to pass AMP configuration to the AnomalyDetector automatically, so that AMP reactive investigation works without additional manual setup.

#### Acceptance Criteria

1. WHEN the controller creates the AnomalyDetector and `amp_workspace_id` or `prometheus_url` is configured in TestConfig, THE Controller SHALL create an AMPMetricCollector and pass it to the AnomalyDetector constructor.
2. WHEN neither `amp_workspace_id` nor `prometheus_url` is configured, THE Controller SHALL pass None for the amp_collector parameter.

### Requirement 6: Testing

**User Story:** As a developer, I want tests that verify AMP reactive investigation behavior, so that I can confirm the feature works correctly and degrades gracefully.

#### Acceptance Criteria

1. WHEN `amp_collector` is configured, THE test suite SHALL verify that AMP queries are attempted during `handle_alert`.
2. WHEN `amp_collector` is None, THE test suite SHALL verify that the investigation completes without errors and without attempting AMP queries.
3. WHEN an AMP query raises an exception, THE test suite SHALL verify that the investigation pipeline continues and produces a valid Finding.
4. WHEN AMP metrics contain threshold violations, THE test suite SHALL verify that the violations appear in the Finding's evidence_references.
