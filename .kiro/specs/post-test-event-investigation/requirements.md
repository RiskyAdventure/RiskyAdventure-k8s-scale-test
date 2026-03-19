# Requirements Document

## Introduction

After a scale test completes and the cluster reaches peak pod count, thousands of Kubernetes warning events accumulate that indicate real cluster health problems (container runtime failures, CNI issues, evictions, OOM kills). The existing anomaly detector only investigates events that coincide with rate drops or pending timeouts. This feature adds a post-test event analysis pass during hold-at-peak that identifies high-volume warning reasons never covered by a rate-drop investigation and runs the full investigation pipeline against them, producing proper Findings with root causes, evidence, and SSM diagnostics.

## Glossary

- **EventWatcher**: The component (`events.py`) that streams Kubernetes events in real time via watch threads and persists them to the EvidenceStore.
- **AnomalyDetector**: The component (`anomaly.py`) that investigates alerts by collecting multi-layer evidence (K8s events, pod phases, stuck nodes, node conditions, AMP metrics, ENI state, SSM diagnostics) and correlating them into a Finding.
- **Finding**: A dataclass (`models.py`) representing the result of an anomaly investigation, containing severity, symptom, root cause, evidence references, and diagnostic data.
- **AlertType**: An enum (`models.py`) that classifies the trigger for an anomaly investigation (e.g., `RATE_DROP`, `PENDING_TIMEOUT`).
- **Controller**: The orchestrator (`controller.py`) that executes the full test lifecycle including scaling, hold-at-peak, health sweep, and cleanup.
- **Phase_7a**: The hold-at-peak phase of the Controller where the cluster is stable at target pod count and health checks run.
- **Warning_Reason**: The `reason` field on a Kubernetes Warning event (e.g., `Failed`, `FailedCreatePodSandBox`, `FailedCreatePodContainer`).
- **Uncovered_Reason**: A Warning_Reason with significant event volume that was not referenced in the evidence_references of any existing Finding from rate-drop or pending-timeout investigations.
- **Chart**: The HTML report (`chart.py`) that displays scaling results, investigation summaries, and the "Investigated?" status for each Warning_Reason.

## Requirements

### Requirement 1: Expose Warning Event Summary from EventWatcher

**User Story:** As the Controller, I want to query the EventWatcher for accumulated warning event counts grouped by reason, so that I can identify which warning patterns need post-test investigation.

#### Acceptance Criteria

1. WHEN the Controller calls `get_warning_summary` on the EventWatcher, THE EventWatcher SHALL return a dictionary keyed by Warning_Reason containing the event count, a sample message, and the involved object kind for each reason.
2. WHEN the Controller calls `get_uncovered_reasons` with a list of existing Findings and a threshold, THE EventWatcher SHALL return only Warning_Reasons whose event count exceeds the threshold and whose reason string does not appear in the evidence_references of any provided Finding.
3. WHEN the threshold parameter is omitted in `get_uncovered_reasons`, THE EventWatcher SHALL default to a threshold of 100 events.
4. WHEN no warning events have been recorded, THE EventWatcher SHALL return an empty dictionary from `get_warning_summary` and an empty list from `get_uncovered_reasons`.

### Requirement 2: Add EVENT_ANALYSIS Alert Type

**User Story:** As the AnomalyDetector, I want a distinct AlertType for post-test event-triggered investigations, so that event analysis findings can be distinguished from rate-drop findings in logs and reports.

#### Acceptance Criteria

1. THE AlertType enum SHALL include an `EVENT_ANALYSIS` member with value `"event_analysis"`.
2. WHEN the AnomalyDetector receives an Alert with `alert_type=AlertType.EVENT_ANALYSIS`, THE AnomalyDetector SHALL log a message that distinguishes the investigation as a post-test event analysis rather than a rate-drop investigation.
3. WHEN the AnomalyDetector processes an `EVENT_ANALYSIS` alert, THE AnomalyDetector SHALL execute the same multi-layer investigation pipeline (K8s events, pod phases, stuck nodes, node conditions, AMP metrics, ENI state, SSM diagnostics) used for other alert types.

### Requirement 3: Run Post-Test Event Analysis in Phase 7a

**User Story:** As a test operator, I want the Controller to automatically investigate high-volume uninvestigated warning patterns after the health sweep completes during hold-at-peak, so that cluster health problems beyond scaling speed are surfaced in the test report.

#### Acceptance Criteria

1. WHEN the health sweep completes during Phase_7a, THE Controller SHALL query the EventWatcher for uncovered warning reasons using the current list of Findings and a threshold of 100 events.
2. WHEN uncovered warning reasons are found, THE Controller SHALL create an Alert with `alert_type=AlertType.EVENT_ANALYSIS` for each uncovered reason, including the reason string, event count, sample message, and namespace list in the alert context.
3. WHEN processing uncovered reasons, THE Controller SHALL cap the number of event-analysis investigations at 3 per test run to keep hold-at-peak extension under 5 minutes.
4. WHEN multiple uncovered reasons exist, THE Controller SHALL process them sequentially rather than concurrently to avoid SSM contention.
5. WHEN an event-analysis investigation produces a Finding, THE Controller SHALL append the Finding to the run's findings list so it appears in the test summary and chart.
6. WHEN `hold_at_peak` is set to 0, THE Controller SHALL still execute the post-test event analysis after the zero-second hold completes and before cleanup begins.
7. IF the EventWatcher returns no uncovered reasons above the threshold, THEN THE Controller SHALL skip the event analysis pass and proceed to cleanup.
8. IF an individual event-analysis investigation raises an exception, THEN THE Controller SHALL log the error and continue with the next uncovered reason rather than aborting the entire analysis pass.

### Requirement 4: Chart Displays Investigation Status for All Warning Reasons

**User Story:** As a test operator reviewing the HTML chart, I want the "Investigated?" column to show "✓ In findings" for all high-volume warning reasons after the post-test analysis, so that I can confirm comprehensive coverage of cluster health issues.

#### Acceptance Criteria

1. WHEN the chart renders the K8s Warning Events table, THE Chart SHALL cross-reference each Warning_Reason against all Findings including those produced by event-analysis investigations.
2. WHEN a Warning_Reason was covered by an event-analysis Finding, THE Chart SHALL display "✓ In findings" in the Investigated column for that reason.

### Requirement 5: Existing Test Suite Compatibility

**User Story:** As a developer, I want all existing tests to continue passing after the changes, so that the new feature does not introduce regressions.

#### Acceptance Criteria

1. WHEN the full test suite is executed with `python3 -m pytest tests/ -v`, THE test suite SHALL report zero failures across all existing tests.
2. WHEN the EventWatcher is used without calling the new methods, THE EventWatcher SHALL behave identically to its pre-change behavior.
