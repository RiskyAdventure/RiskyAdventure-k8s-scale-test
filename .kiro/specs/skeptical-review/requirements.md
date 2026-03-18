# Requirements Document

## Introduction

The Skeptical Review feature adds independent re-verification of findings produced by the AnomalyDetector during scale tests. Currently, the AnomalyDetector writes findings to `findings/*.json` but nothing independently verifies them. A wrong early conclusion can propagate through the investigation chain unchallenged. The FindingReviewer class takes a finding, re-queries the underlying data sources using a different approach, checks for staleness, assigns a confidence level, lists alternative explanations, and appends a `review` field to the finding JSON.

## Glossary

- **Finding**: A dataclass (`models.Finding`) representing the result of an anomaly investigation, containing severity, symptom, evidence references, root cause, and affected resources.
- **Review**: A JSON object appended to a Finding that contains confidence assessment, reasoning, alternative explanations, checkpoint questions, and verification results.
- **FindingReviewer**: A new class in `reviewer.py` that independently re-verifies findings by re-querying data sources.
- **AMPMetricCollector**: An existing class in `health_sweep.py` that executes PromQL queries against AMP/Prometheus endpoints with SigV4 signing.
- **Confidence_Level**: One of "high", "medium", or "low" indicating the reviewer's assessment of a finding's validity.
- **Verification_Result**: A single claim-level check within a review, containing the claim text, whether it was verified, and supporting detail.
- **Staleness**: A condition where the data underlying a finding may no longer reflect the current cluster state because sufficient time has elapsed since the finding was produced.
- **Controller**: The `ScaleTestController` class in `controller.py` that orchestrates the test lifecycle and calls `anomaly.handle_alert()`.
- **EvidenceStore**: The persistence layer that saves findings and other test artifacts to disk.

## Requirements

### Requirement 1: FindingReviewer Class Structure

**User Story:** As a scale test operator, I want an independent reviewer for anomaly findings, so that wrong conclusions do not propagate unchallenged through the investigation chain.

#### Acceptance Criteria

1. THE FindingReviewer SHALL accept a Finding object, an AMPMetricCollector instance (or None), and a CloudWatch executor function (or None) as inputs to its review method
2. THE FindingReviewer SHALL be instantiated with an EvidenceStore and run_id for persisting updated findings
3. WHEN the FindingReviewer is instantiated without an AMPMetricCollector and without a CloudWatch executor, THE FindingReviewer SHALL still produce a valid review using only the evidence already present in the Finding

### Requirement 2: Central Claim Identification

**User Story:** As a scale test operator, I want the reviewer to identify the central claim in each finding, so that the re-verification is focused on the most important assertion.

#### Acceptance Criteria

1. WHEN a Finding has a non-None root_cause, THE FindingReviewer SHALL use the root_cause string as the central claim
2. WHEN a Finding has a None root_cause, THE FindingReviewer SHALL use the symptom string as the central claim
3. THE FindingReviewer SHALL include the identified central claim in the review's verification_results list

### Requirement 3: Independent Re-Query

**User Story:** As a scale test operator, I want the reviewer to re-query data sources using a different approach than the original investigation, so that the finding is validated from an independent angle.

#### Acceptance Criteria

1. WHEN an AMPMetricCollector is available and the Finding's evidence_references contain AMP-related entries, THE FindingReviewer SHALL execute at least one PromQL query that differs from the original investigation queries (e.g., an instant query at the finding timestamp instead of a range query)
2. WHEN a CloudWatch executor is available and the Finding's evidence_references contain CloudWatch-related entries, THE FindingReviewer SHALL execute at least one CloudWatch Logs Insights query scoped to the finding's timestamp
3. WHEN neither an AMPMetricCollector nor a CloudWatch executor is available, THE FindingReviewer SHALL base the review solely on the evidence already present in the Finding (K8s events, node diagnostics, evidence_references)
4. THE FindingReviewer SHALL record each re-query result as a Verification_Result in the review

### Requirement 4: Staleness Detection

**User Story:** As a scale test operator, I want the reviewer to detect when a finding's underlying conditions may have changed, so that I know whether the finding still reflects the current cluster state.

#### Acceptance Criteria

1. WHEN the elapsed time between the Finding's timestamp and the current time exceeds a configurable staleness threshold (default 300 seconds), THE FindingReviewer SHALL mark the review as potentially stale in the reasoning field
2. WHEN the Finding is marked as potentially stale, THE FindingReviewer SHALL reduce the confidence level by one step (high to medium, medium to low)
3. THE FindingReviewer SHALL include the staleness assessment in the review's reasoning field

### Requirement 5: Confidence Assignment

**User Story:** As a scale test operator, I want each reviewed finding to have a confidence level, so that I can prioritize which findings to act on.

#### Acceptance Criteria

1. WHEN all verification results confirm the central claim and no staleness is detected, THE FindingReviewer SHALL assign confidence "high"
2. WHEN some verification results confirm the central claim but others are inconclusive or contradictory, THE FindingReviewer SHALL assign confidence "medium"
3. WHEN verification results contradict the central claim or significant evidence gaps exist, THE FindingReviewer SHALL assign confidence "low"
4. THE FindingReviewer SHALL include the confidence level in the review field as one of "high", "medium", or "low"

### Requirement 6: Alternative Explanations

**User Story:** As a scale test operator, I want the reviewer to suggest alternative explanations for the observed symptoms, so that I can consider other possible root causes.

#### Acceptance Criteria

1. THE FindingReviewer SHALL generate a list of alternative explanations based on the Finding's K8s events, evidence_references, and re-query results
2. WHEN the Finding's root_cause references a known KB entry, THE FindingReviewer SHALL check the KB entry's alternative_explanations field and include relevant alternatives
3. THE FindingReviewer SHALL include the alternative explanations list in the review field

### Requirement 7: Checkpoint Questions

**User Story:** As a scale test operator, I want the reviewer to generate actionable checkpoint questions when confidence is not high, so that I know what to investigate next.

#### Acceptance Criteria

1. WHEN the review confidence is "medium" or "low", THE FindingReviewer SHALL generate at least one checkpoint question
2. THE FindingReviewer SHALL make checkpoint questions specific and actionable (referencing concrete resources, metrics, or log sources from the finding)
3. WHEN the review confidence is "high", THE FindingReviewer SHALL set checkpoint_questions to an empty list
4. THE FindingReviewer SHALL include the checkpoint questions in the review field

### Requirement 8: Review Field Schema

**User Story:** As a scale test operator, I want the review output to follow a consistent schema, so that downstream tooling can parse and display reviews reliably.

#### Acceptance Criteria

1. THE FindingReviewer SHALL produce a review dict matching the schema defined in the steering file: confidence (string), reasoning (string), alternative_explanations (list of strings), checkpoint_questions (list of strings), and verification_results (list of dicts with claim, verified, and detail fields)
2. THE FindingReviewer SHALL append the review dict under the key "review" to the Finding's serialized JSON
3. THE FindingReviewer SHALL persist the updated Finding JSON to the EvidenceStore

### Requirement 9: Asynchronous Integration with Controller

**User Story:** As a scale test operator, I want the review to run asynchronously after each finding is produced, so that the scaling loop is not blocked by the review process.

#### Acceptance Criteria

1. WHEN the AnomalyDetector's handle_alert method returns a Finding, THE Controller SHALL schedule the FindingReviewer's review as an asynchronous task that does not block the scaling loop
2. IF the FindingReviewer raises an exception during review, THEN THE Controller SHALL log the error and continue the scaling loop without interruption
3. THE Controller SHALL pass the same AMPMetricCollector and CloudWatch executor instances used by the ObservabilityScanner to the FindingReviewer
4. WHEN the test run completes cleanup, THE Controller SHALL await all pending review tasks before generating the summary

### Requirement 10: Graceful Degradation

**User Story:** As a scale test operator, I want the review process to degrade gracefully when data sources are unavailable, so that partial reviews are still produced rather than no review at all.

#### Acceptance Criteria

1. IF an AMP query fails during review, THEN THE FindingReviewer SHALL log the error, record the failure in the corresponding Verification_Result, and continue with remaining checks
2. IF a CloudWatch query fails during review, THEN THE FindingReviewer SHALL log the error, record the failure in the corresponding Verification_Result, and continue with remaining checks
3. IF all re-queries fail, THEN THE FindingReviewer SHALL still produce a valid review based on the evidence already present in the Finding, with confidence reduced to "low"
