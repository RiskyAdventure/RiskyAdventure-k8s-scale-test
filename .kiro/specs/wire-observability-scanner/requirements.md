# Requirements Document

## Introduction

Wire the existing `ObservabilityScanner` module into the `ScaleTestController` lifecycle. The scanner is fully implemented in `observability.py` but not yet integrated into the controller's test run phases. This feature connects the scanner to the controller so it runs proactive PromQL and CloudWatch Logs Insights queries during scaling and hold-at-peak phases, feeding early-warning findings into the existing findings pipeline and test run summary.

## Glossary

- **Controller**: The `ScaleTestController` class in `controller.py` that orchestrates the full scale test lifecycle.
- **Scanner**: The `ObservabilityScanner` class in `observability.py` that runs periodic observability queries.
- **Phase**: An enum (`Phase.SCALING`, `Phase.HOLD_AT_PEAK`) indicating which stage of the test the scanner is operating in.
- **ScanResult**: A dataclass produced by the scanner representing a detected anomaly with severity, title, detail, and optional drill-down source.
- **Finding**: A dataclass in `models.py` used by the anomaly detector and collected in `Controller._findings`.
- **AMP**: Amazon Managed Prometheus, accessed via `AMPMetricCollector._query_promql` for PromQL queries.
- **CloudWatch_Executor**: A callable that runs CloudWatch Logs Insights queries against a configured log group.
- **Prometheus_Executor**: A callable that runs a PromQL instant query and returns the raw Prometheus JSON response.
- **Evidence_Store**: The persistence layer that saves test run artifacts (findings, summaries, etc.) to disk.
- **Catalog**: The list of `ScanQuery` definitions that the scanner iterates over each scan cycle.
- **TestConfig**: The configuration dataclass holding all test parameters including `amp_workspace_id`, `prometheus_url`, `cloudwatch_log_group`, and `eks_cluster_name`.

## Requirements

### Requirement 1: Scanner Instantiation

**User Story:** As a test operator, I want the controller to create an ObservabilityScanner instance with the correct executor functions, so that the scanner can query Prometheus and CloudWatch during the test run.

#### Acceptance Criteria

1. WHEN the controller enters the monitoring setup phase, THE Controller SHALL create an ObservabilityScanner instance with a Prometheus_Executor wrapping `AMPMetricCollector._query_promql` and a CloudWatch_Executor wrapping CloudWatch Logs Insights.
2. WHEN `TestConfig.amp_workspace_id` is set or `TestConfig.prometheus_url` is set, THE Controller SHALL provide a non-null Prometheus_Executor to the Scanner.
3. WHEN neither `TestConfig.amp_workspace_id` nor `TestConfig.prometheus_url` is set, THE Controller SHALL provide a null Prometheus_Executor to the Scanner.
4. WHEN `TestConfig.cloudwatch_log_group` is set and `TestConfig.eks_cluster_name` is set, THE Controller SHALL provide a non-null CloudWatch_Executor to the Scanner.
5. WHEN `TestConfig.cloudwatch_log_group` is not set or `TestConfig.eks_cluster_name` is not set, THE Controller SHALL provide a null CloudWatch_Executor to the Scanner.
6. WHEN both Prometheus_Executor and CloudWatch_Executor are null, THE Controller SHALL skip scanner creation entirely and log a message explaining the skip reason.

### Requirement 2: Scanner Lifecycle Management

**User Story:** As a test operator, I want the scanner to start before scaling begins and stop reliably in the finally block, so that the scanner covers the entire scaling and hold-at-peak window without leaking async tasks.

#### Acceptance Criteria

1. WHEN the controller has completed monitoring setup and before `_execute_scaling_via_flux` is called, THE Controller SHALL start the Scanner as an asyncio task via `asyncio.create_task(scanner.run())`.
2. WHEN the controller enters the finally block after scaling and hold-at-peak, THE Controller SHALL call `scanner.stop()` and await the scanner task to ensure clean shutdown.
3. IF the scanner task raises an unhandled exception, THEN THE Controller SHALL log the exception and continue with the remaining cleanup steps.
4. WHEN the scanner is not created due to missing configuration, THE Controller SHALL proceed with the test run without the scanner, and all other monitoring components SHALL operate unchanged.

### Requirement 3: Phase Transitions

**User Story:** As a test operator, I want the scanner to know which test phase is active, so that it runs the correct subset of catalog queries for each phase.

#### Acceptance Criteria

1. WHEN `_execute_scaling_via_flux` begins, THE Controller SHALL call `scanner.set_phase(Phase.SCALING)`.
2. WHEN the hold-at-peak period begins, THE Controller SHALL call `scanner.set_phase(Phase.HOLD_AT_PEAK)`.

### Requirement 4: Context Updates During Scaling

**User Story:** As a test operator, I want the scanner to receive live pod counts and elapsed time each polling iteration, so that catalog query conditions and evaluators have current data.

#### Acceptance Criteria

1. WHEN the scaling loop polls pod counts each iteration, THE Controller SHALL call `scanner.update_context(elapsed_minutes=..., pending=..., ready=...)` with the current values.
2. THE Controller SHALL pass `elapsed_minutes` as a float representing minutes elapsed since scaling started.
3. THE Controller SHALL pass `pending` as the integer count of pending pods from the current poll.
4. THE Controller SHALL pass `ready` as the integer count of ready pods from the current poll.

### Requirement 5: Finding Callback and Persistence

**User Story:** As a test operator, I want scanner findings to be logged, persisted to the evidence store, and collected alongside anomaly findings, so that all observability data is available in the test run summary.

#### Acceptance Criteria

1. WHEN the Scanner is created, THE Controller SHALL register a finding callback via `scanner.on_finding(callback)`.
2. WHEN the callback receives a ScanResult, THE Controller SHALL log the finding at warning level with the scanner query name, severity, and title.
3. WHEN the callback receives a ScanResult, THE Controller SHALL persist the finding to the Evidence_Store.
4. WHEN the callback receives a ScanResult, THE Controller SHALL append the finding to a scanner-specific findings list that is distinguishable from anomaly findings.
5. IF the finding callback raises an exception, THEN THE Controller SHALL log the error and continue scanner operation without interruption.

### Requirement 6: Summary Inclusion

**User Story:** As a test operator, I want scanner findings included in the final TestRunSummary, so that the test report contains all proactive observability data.

#### Acceptance Criteria

1. WHEN `_make_summary` builds the TestRunSummary, THE Controller SHALL include scanner findings in the summary.
2. THE Controller SHALL keep scanner findings distinguishable from anomaly detector findings in the summary data.

### Requirement 7: Module Boundary Preservation

**User Story:** As a developer, I want the scanner integration to respect existing module boundaries, so that monitor, health sweep, anomaly detector, infra health, and event watcher continue to operate independently.

#### Acceptance Criteria

1. THE Controller SHALL NOT modify `monitor.py`, `health_sweep.py`, `anomaly.py`, `infra_health.py`, or `events.py` as part of scanner integration.
2. WHEN the Scanner is running, THE PodRateMonitor SHALL continue to measure pod ready rates independently using its own watch-based mechanism.
3. WHEN the Scanner is running, THE HealthSweepAgent SHALL continue to run its own per-node PromQL queries independently at hold-at-peak.
4. WHEN the Scanner is running, THE AnomalyDetector SHALL continue to handle alerts and run investigations independently.

### Requirement 8: Graceful Degradation

**User Story:** As a test operator, I want the scanner to degrade gracefully when backends are unavailable, so that scanner failures do not block or disrupt the test run.

#### Acceptance Criteria

1. IF the Prometheus_Executor raises an exception during a query, THEN THE Scanner SHALL log the error and continue with the next scheduled query.
2. IF the CloudWatch_Executor raises an exception during a query, THEN THE Scanner SHALL log the error and continue with the next scheduled query.
3. IF the scanner asyncio task fails entirely, THEN THE Controller SHALL log the failure and complete the test run without scanner data.
