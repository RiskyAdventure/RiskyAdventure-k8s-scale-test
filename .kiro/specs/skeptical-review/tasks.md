# Implementation Plan: Skeptical Review — Independent Finding Re-verification

## Overview

Implement the `FindingReviewer` class in a new file `src/k8s_scale_test/reviewer.py`, integrate it into the controller's alert handling flow as an async fire-and-forget task, and write tests in `tests/test_reviewer.py`. The implementation builds on the existing AnomalyDetector → Finding pipeline and reuses the AMPMetricCollector and CloudWatch executor already constructed by the controller.

## Tasks

- [x] 1. Create FindingReviewer core class with claim identification and confidence logic
  - [x] 1.1 Create `src/k8s_scale_test/reviewer.py` with the FindingReviewer class skeleton
    - Constructor accepting evidence_store, run_id, amp_collector (optional), cloudwatch_fn (optional), staleness_threshold_seconds (default 300)
    - Implement `_identify_claim(finding)`: return root_cause if non-None, else symptom
    - Implement `_check_staleness(finding)`: compare finding.timestamp to now, return (is_stale, reasoning_text)
    - Implement `_assign_confidence(verification_results, is_stale)`: apply the confidence rules (all verified + not stale → high, mixed → medium, contradicted/gaps → low, staleness downgrades by one step)
    - Implement `_build_review(confidence, reasoning, alternatives, checkpoints, verification_results)`: assemble the review dict matching the steering file schema
    - _Requirements: 1.1, 1.2, 2.1, 2.2, 4.1, 4.2, 4.3, 5.1, 5.2, 5.3, 5.4, 8.1_

  - [ ]* 1.2 Write property tests for claim identification and confidence assignment
    - **Property 1: Central claim identification**
    - **Validates: Requirements 2.1, 2.2**
    - **Property 5: Confidence assignment correctness**
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.4**

- [x] 2. Implement re-query logic and review pipeline
  - [x] 2.1 Implement `_requery_amp(finding)` in FindingReviewer
    - Parse evidence_references for AMP-related entries (e.g., "amp:" prefix)
    - Construct different PromQL queries (instant queries with different rate windows)
    - Call amp_collector._query_promql() for each query
    - Return list of verification_result dicts
    - Catch exceptions per-query, record failures in verification_results
    - _Requirements: 3.1, 3.4, 10.1_

  - [x] 2.2 Implement `_requery_cloudwatch(finding)` in FindingReviewer
    - Parse evidence_references for warning event patterns (e.g., "warnings:" prefix)
    - Construct CloudWatch Logs Insights queries with ±1 minute window around finding timestamp
    - Call cloudwatch_fn with the query and time bounds
    - Return list of verification_result dicts
    - Catch exceptions, record failures in verification_results
    - _Requirements: 3.2, 3.4, 10.2_

  - [x] 2.3 Implement `_generate_alternatives(finding, verification_results)` in FindingReviewer
    - Generate alternative explanations based on K8s event patterns, evidence_references, and re-query results
    - Check for KB entry references in evidence_references and include KB alternative_explanations if available
    - _Requirements: 6.1, 6.2, 6.3_

  - [x] 2.4 Implement `_generate_checkpoint_questions(finding, confidence, verification_results)` in FindingReviewer
    - Generate actionable questions referencing concrete resources/metrics from the finding
    - Return non-empty list when confidence is "medium" or "low", empty list when "high"
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

  - [x] 2.5 Implement `_persist_review(finding, review)` in FindingReviewer
    - Serialize finding via finding.to_dict(), append "review" key, write to EvidenceStore
    - _Requirements: 8.2, 8.3_

  - [x] 2.6 Implement the main `review(finding)` async method that orchestrates the full pipeline
    - Call _identify_claim, _requery_amp, _requery_cloudwatch, _check_staleness, _assign_confidence, _generate_alternatives, _generate_checkpoint_questions, _build_review, _persist_review
    - Handle all-queries-failed case: set confidence to "low"
    - _Requirements: 1.3, 3.3, 10.3_

  - [ ]* 2.7 Write property tests for review pipeline
    - **Property 2: Valid review without external data sources**
    - **Validates: Requirements 1.3, 3.3, 8.1**
    - **Property 3: Re-query execution when sources available**
    - **Validates: Requirements 3.1, 3.2**
    - **Property 4: Staleness detection and confidence downgrade**
    - **Validates: Requirements 4.1, 4.2, 4.3**
    - **Property 6: Checkpoint questions conditional on confidence**
    - **Validates: Requirements 7.1, 7.3**
    - **Property 8: Graceful degradation on query failure**
    - **Validates: Requirements 10.1, 10.2, 10.3**

- [x] 3. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Integrate FindingReviewer into the controller
  - [x] 4.1 Wire FindingReviewer into ScaleTestController
    - Add `self._reviewer: FindingReviewer | None = None` and `self._review_tasks: list[asyncio.Task] = []` to `__init__`
    - In `run()`, after scanner/AMP setup, create FindingReviewer with the same amp_collector and cloudwatch_fn used by the scanner
    - Add `_safe_review(finding)` method that wraps `self._reviewer.review(finding)` in try/except
    - After each `anomaly.handle_alert()` call site (3 locations in controller.py), add `asyncio.create_task(self._safe_review(finding))` and append to `self._review_tasks`
    - In cleanup (before `_make_summary`), add `await asyncio.gather(*self._review_tasks, return_exceptions=True)`
    - _Requirements: 9.1, 9.2, 9.3, 9.4_

  - [ ]* 4.2 Write property test for controller exception isolation
    - **Property 9: Controller exception isolation**
    - **Validates: Requirements 9.2**

  - [ ]* 4.3 Write unit tests for controller integration
    - Structural test: `inspect.getsource()` verifying FindingReviewer creation in controller
    - Structural test: `inspect.getsource()` verifying `asyncio.create_task` after handle_alert
    - Structural test: `inspect.getsource()` verifying `asyncio.gather(*self._review_tasks` before summary
    - _Requirements: 9.1, 9.3, 9.4_

- [x] 5. Implement persistence round-trip test
  - [ ]* 5.1 Write property test for review persistence round-trip
    - **Property 7: Review persistence round-trip**
    - **Validates: Requirements 8.2, 8.3**

- [x] 6. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties
- Unit tests validate specific examples and edge cases
- Run tests with: `python3 -m pytest tests/test_reviewer.py -v`
- Skip known failures: `python3 -m pytest tests/ --ignore=tests/test_controller_observability_prompt.py -v`
