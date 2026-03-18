# Requirements Document

## Introduction

The ObservabilityScanner and AnomalyDetector operate independently during scale tests. The scanner proactively queries AMP/CloudWatch every 15-30 seconds and produces `ScanResult` findings (CPU pressure, pending pod backlogs, Karpenter errors, etc.). The anomaly detector reactively investigates when the monitor fires an alert (rate drop, pending timeout, monitor gap). When a rate drop occurs 30 seconds after the scanner already detected CPU pressure on the same nodes, the anomaly detector starts from scratch — it has no knowledge of what the scanner already found.

This feature introduces a shared in-memory context that both modules read and write, enabling the anomaly detector to reference existing scanner findings instead of re-investigating known causes. The scanner writes its findings to the shared context; the anomaly detector reads them before starting its investigation pipeline. If a scanner finding matches the alert's symptoms (overlapping time window and affected resources), the anomaly detector references it as prior evidence rather than re-collecting the same data.

Thread safety is a key concern: the scanner runs as an asyncio background task, and the anomaly detector runs in the event loop via alert callbacks. An asyncio-safe data structure or lock is required to prevent concurrent read/write corruption.

## Glossary

- **ObservabilityScanner**: The proactive monitoring module (`observability.py`) that runs fleet-wide PromQL and CloudWatch queries in a background asyncio task during scaling and hold-at-peak phases.
- **AnomalyDetector**: The reactive investigation module (`anomaly.py`) that runs a multi-layer evidence collection pipeline when the monitor fires an alert.
- **ScanResult**: The dataclass (`observability.py`) representing a scanner finding, containing query_name, severity, title, detail, source, raw_result, and optional drill_down fields.
- **Finding**: The dataclass (`models.py`) representing an anomaly investigation result, containing severity, root_cause, evidence_references, K8s events, node metrics, and node diagnostics.
- **Alert**: The dataclass (`models.py`) representing a rate drop, pending timeout, or monitor gap event that triggers investigation.
- **SharedContext**: The new in-memory data structure that stores scanner findings and allows the anomaly detector to query them by time window and affected resources.
- **ContextFileWriter**: The existing module (`agent_context.py`) that writes `agent_context.json` to disk for AI sub-agent integration.
- **Correlation_Window**: The maximum time difference (in seconds) between a scanner finding and an alert for them to be considered related.
- **Affected_Resources**: Node names, pod names, or query names that identify which cluster resources a finding or alert pertains to.

## Requirements

### Requirement 1: Shared Context Data Structure

**User Story:** As a scale test operator, I want a shared in-memory context that both the scanner and anomaly detector can access, so that scanner findings are available to the anomaly detector without re-querying the same data sources.

#### Acceptance Criteria

1. THE SharedContext SHALL store ScanResult entries with their associated timestamps.
2. THE SharedContext SHALL support concurrent reads and writes from asyncio tasks using an asyncio-safe synchronization mechanism.
3. WHEN a ScanResult is added to the SharedContext, THE SharedContext SHALL record the ScanResult along with the timestamp at which the finding was produced.
4. THE SharedContext SHALL provide a query method that returns ScanResult entries within a specified time window relative to a given timestamp.
5. THE SharedContext SHALL evict ScanResult entries older than a configurable maximum age to bound memory usage.

### Requirement 2: Scanner Writes Findings to SharedContext

**User Story:** As a scale test operator, I want the ObservabilityScanner to write its findings to the SharedContext, so that downstream modules can access scanner results without coupling to the scanner directly.

#### Acceptance Criteria

1. WHEN the ObservabilityScanner produces a ScanResult finding, THE ObservabilityScanner SHALL write the finding to the SharedContext.
2. WHEN writing to the SharedContext, THE ObservabilityScanner SHALL include the query_name, severity, title, detail, and source fields from the ScanResult.
3. WHEN the SharedContext is not configured (None), THE ObservabilityScanner SHALL continue operating without writing to the SharedContext.

### Requirement 3: Anomaly Detector Reads SharedContext Before Investigation

**User Story:** As a scale test operator, I want the anomaly detector to check the SharedContext for relevant scanner findings before starting its investigation, so that known causes are referenced instead of re-investigated.

#### Acceptance Criteria

1. WHEN an alert triggers investigation and a SharedContext is configured, THE AnomalyDetector SHALL query the SharedContext for ScanResult entries within the Correlation_Window before starting the full investigation pipeline.
2. WHEN querying the SharedContext, THE AnomalyDetector SHALL use the alert timestamp and a configurable Correlation_Window (default 120 seconds) to find temporally relevant scanner findings.
3. WHEN the SharedContext is not configured (None), THE AnomalyDetector SHALL skip the SharedContext lookup and proceed with the full investigation pipeline.

### Requirement 4: Correlation Matching Between Scanner Findings and Alerts

**User Story:** As a scale test operator, I want the anomaly detector to match scanner findings to alerts based on time window and affected resources, so that only relevant scanner findings are referenced.

#### Acceptance Criteria

1. WHEN the AnomalyDetector retrieves scanner findings from the SharedContext, THE AnomalyDetector SHALL consider a scanner finding as matching if the finding timestamp falls within the Correlation_Window of the alert timestamp.
2. WHEN a scanner finding's affected resources (node names extracted from the finding detail or title) overlap with the alert's affected resources (namespaces, node names from stuck pods or K8s events), THE AnomalyDetector SHALL treat the finding as a strong match.
3. WHEN a scanner finding matches temporally but has no resource overlap information, THE AnomalyDetector SHALL treat the finding as a weak match and still reference it as prior evidence.
4. FOR ALL scanner findings and alerts, the matching function SHALL be deterministic: given the same inputs, the matching function SHALL produce the same output.

### Requirement 5: Integration of Matched Findings into Investigation

**User Story:** As a scale test operator, I want matched scanner findings included in the anomaly detector's Finding output, so that the investigation report shows what the scanner already detected.

#### Acceptance Criteria

1. WHEN one or more scanner findings match the alert, THE AnomalyDetector SHALL add scanner finding references to the Finding's evidence_references list with a "scanner:" prefix.
2. WHEN a scanner finding with severity WARNING or CRITICAL matches the alert, THE AnomalyDetector SHALL incorporate the scanner finding's title into the root cause extraction as a contributing clue.
3. WHEN a strong-match scanner finding already identifies the root cause (severity CRITICAL and matching resource overlap), THE AnomalyDetector SHALL reference the scanner finding in the root cause and MAY skip redundant investigation layers that would collect the same evidence.
4. WHEN no scanner findings match the alert, THE AnomalyDetector SHALL proceed with the full investigation pipeline without modification.

### Requirement 6: Controller Wiring

**User Story:** As a scale test operator, I want the controller to create the SharedContext and pass it to both the scanner and anomaly detector, so that cross-source correlation works without additional manual setup.

#### Acceptance Criteria

1. WHEN the controller creates both an ObservabilityScanner and an AnomalyDetector, THE Controller SHALL create a single SharedContext instance and pass it to both modules.
2. WHEN the ObservabilityScanner is not created (no AMP/CloudWatch configured), THE Controller SHALL pass None for the SharedContext parameter to the AnomalyDetector.
3. THE Controller SHALL configure the SharedContext with a maximum entry age matching the test's event_time_window_minutes configuration value.

### Requirement 7: Graceful Degradation

**User Story:** As a scale test operator, I want SharedContext failures to degrade gracefully, so that neither the scanner nor the anomaly detector is disrupted by SharedContext errors.

#### Acceptance Criteria

1. IF a write to the SharedContext fails, THEN THE ObservabilityScanner SHALL log the error and continue scanning without interruption.
2. IF a read from the SharedContext fails, THEN THE AnomalyDetector SHALL log the error and proceed with the full investigation pipeline.
3. IF the SharedContext is None, THEN both the ObservabilityScanner and AnomalyDetector SHALL operate identically to their current behavior without the SharedContext.

### Requirement 8: Testing

**User Story:** As a developer, I want property-based and unit tests that verify the SharedContext and correlation matching logic, so that I can confirm correct behavior across a wide range of scanner findings and alerts.

#### Acceptance Criteria

1. THE test suite SHALL include property-based tests that generate random ScanResult findings and Alert objects and verify that the correlation matching function correctly identifies matches based on time window and affected resource overlap.
2. THE test suite SHALL verify that the SharedContext evicts entries older than the configured maximum age.
3. THE test suite SHALL verify that concurrent reads and writes to the SharedContext do not corrupt data or raise exceptions.
4. THE test suite SHALL verify that the matching function is deterministic: calling the matching function twice with the same inputs produces the same output.
5. THE test suite SHALL verify that the AnomalyDetector produces a valid Finding regardless of whether the SharedContext contains matching findings, no findings, or is None.
