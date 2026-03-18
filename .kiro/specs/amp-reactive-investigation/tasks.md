# Implementation Plan: AMP Reactive Investigation

## Overview

Add an AMP/Prometheus metric query layer to the AnomalyDetector's reactive investigation pipeline. The implementation modifies `anomaly.py` (new `_collect_amp_metrics` method, updated `handle_alert`, `_correlate`, `_extract_root_cause`), `controller.py` (wiring), and adds tests in `tests/test_anomaly_amp.py`.

## Tasks

- [x] 1. Add amp_collector parameter to AnomalyDetector
  - [x] 1.1 Add optional `amp_collector` parameter to `AnomalyDetector.__init__` and store it as `self.amp_collector`
    - Add import for `NodeMetricResult`, `check_threshold` from `health_sweep`
    - Update the class docstring to document the new parameter
    - _Requirements: 1.1, 1.2, 1.3_

  - [ ]* 1.2 Write unit tests for constructor amp_collector handling
    - Test that amp_collector is stored when passed
    - Test that amp_collector defaults to None when not passed
    - Test that existing tests still pass (no regression)
    - _Requirements: 1.1, 1.2, 1.3_

- [x] 2. Implement AMP metric collection in handle_alert
  - [x] 2.1 Add `_collect_amp_metrics` method to AnomalyDetector
    - Calls `self.amp_collector.collect_all()` wrapped in try/except
    - Returns `dict[str, list[NodeMetricResult]]` — empty dict on failure
    - Logs errors at WARNING level on exception
    - _Requirements: 2.1, 2.2, 2.4, 4.1_

  - [x] 2.2 Insert AMP query layer into `handle_alert` pipeline
    - Add after Layer 4 (stuck nodes + conditions), before Layer 5 (ENI)
    - Wrap in `with span("investigation/amp_metrics")`
    - Only execute when `self.amp_collector is not None`
    - Pass `amp_metrics` to `_correlate` call
    - _Requirements: 2.1, 4.2_

  - [ ]* 2.3 Write property test: AMP queries attempted when collector is configured
    - **Property 1: AMP queries attempted when collector is configured**
    - **Validates: Requirements 2.1**

- [x] 3. Integrate AMP evidence into correlation and root cause
  - [x] 3.1 Update `_correlate` to accept and process `amp_metrics` parameter
    - Add `amp_metrics=None` parameter
    - Add AMP evidence to `evidence_refs` list: violation count when thresholds exceeded, checked count when clean, "amp:no_data" when empty and collector configured
    - Pass `amp_metrics` to `_extract_root_cause`
    - _Requirements: 3.1, 3.2, 4.3_

  - [x] 3.2 Update `_extract_root_cause` to incorporate AMP metric clues
    - Add `amp_metrics=None` parameter
    - For each category with threshold violations, append "AMP: {metric} {value} on {node}" clue
    - Thresholds: CPU > 90%, memory > 90%, network_errors > 0, pod_restarts > 5
    - _Requirements: 3.3, 3.4_

  - [ ]* 3.3 Write property test: AMP evidence in evidence_references reflects metric content
    - **Property 2: AMP evidence in evidence_references reflects metric content**
    - **Validates: Requirements 3.1, 3.2**

  - [ ]* 3.4 Write property test: AMP threshold violations appear in root cause
    - **Property 3: AMP threshold violations appear in root cause**
    - **Validates: Requirements 3.3, 3.4**

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - Run: `python3 -m pytest tests/ --ignore=tests/test_controller_observability_prompt.py -v`

- [x] 5. Wire AMP collector in controller
  - [x] 5.1 Update controller to create AMPMetricCollector and pass to AnomalyDetector
    - In the monitoring setup section where AnomalyDetector is constructed
    - Create AMPMetricCollector when `amp_workspace_id` or `prometheus_url` is configured
    - Wrap construction in try/except, pass None on failure
    - Pass as `amp_collector=` keyword argument
    - _Requirements: 5.1, 5.2_

  - [ ]* 5.2 Write unit tests for controller wiring
    - Test that amp_collector is passed when amp_workspace_id is configured
    - Test that None is passed when neither amp_workspace_id nor prometheus_url is configured
    - _Requirements: 5.1, 5.2_

- [x] 6. Graceful degradation tests
  - [ ]* 6.1 Write property test: Graceful degradation on AMP failure
    - **Property 4: Graceful degradation on AMP failure**
    - **Validates: Requirements 4.1**

  - [ ]* 6.2 Write unit test for empty AMP results edge case
    - Mock collect_all to return empty dicts for all categories
    - Verify "amp:no_data" or equivalent in evidence_references
    - _Requirements: 4.3_

- [x] 7. Update module docstring and documentation
  - [x] 7.1 Update `anomaly.py` module docstring to include AMP layer in the investigation pipeline description
    - Add Layer 4.5 (AMP metrics) to the docstring's investigation layers list
    - _Requirements: 2.1_

- [x] 8. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - Run: `python3 -m pytest tests/ --ignore=tests/test_controller_observability_prompt.py -v`

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Property tests validate universal correctness properties using hypothesis
- Unit tests validate specific examples and edge cases
- The existing `AMPMetricCollector` and `check_threshold` from `health_sweep.py` are reused — no new data models needed
