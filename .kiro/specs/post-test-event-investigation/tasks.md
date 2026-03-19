# Implementation Plan: Post-Test Event Investigation

## Overview

Add a post-test event analysis pass that investigates high-volume uninvestigated K8s warning events during hold-at-peak. Changes span four files: `models.py` (new enum member), `events.py` (two new methods), `anomaly.py` (logging tweak), and `controller.py` (orchestration in Phase 7a). Property tests validate the EventWatcher methods; unit tests cover the enum, edge cases, and controller integration.

## Tasks

- [x] 1. Add EVENT_ANALYSIS alert type and EventWatcher methods
  - [x] 1.1 Add `EVENT_ANALYSIS = "event_analysis"` to the `AlertType` enum in `src/k8s_scale_test/models.py`
    - _Requirements: 2.1_
  - [x] 1.2 Implement `get_warning_summary()` on `EventWatcher` in `src/k8s_scale_test/events.py`
    - Read `events.jsonl` from the EvidenceStore, group Warning events by reason
    - Return `{reason: {count, sample_message, kind}}` dict
    - Return empty dict if no events exist
    - _Requirements: 1.1, 1.4_
  - [x] 1.3 Implement `get_uncovered_reasons()` on `EventWatcher` in `src/k8s_scale_test/events.py`
    - Accept `findings: list[Finding]` and `threshold: int = 100`
    - Cross-reference warning summary against findings' `evidence_references` (parse `warnings:Reason=N` patterns) and `k8s_events` reason fields
    - Return `list[tuple[str, dict]]` sorted by count descending, filtered to count > threshold and not covered
    - _Requirements: 1.2, 1.3, 1.4_
  - [x]* 1.4 Write property test for `get_warning_summary()` (Property 1)
    - **Property 1: Warning summary correctly aggregates events by reason**
    - Generate random K8sEvent lists with varied reasons, messages, kinds, event_types
    - Write to temp events.jsonl, call get_warning_summary, verify aggregation invariants
    - **Validates: Requirements 1.1**
  - [x]* 1.5 Write property test for `get_uncovered_reasons()` (Property 2)
    - **Property 2: Uncovered reasons filtering is correct and complete**
    - Generate random events and Finding objects with varied evidence_references
    - Verify completeness (nothing missing) and soundness (nothing spurious)
    - **Validates: Requirements 1.2**
  - [x]* 1.6 Write unit tests for edge cases and enum
    - Test `AlertType.EVENT_ANALYSIS` exists with value `"event_analysis"`
    - Test `get_warning_summary()` returns empty dict with no events
    - Test `get_warning_summary()` excludes Normal events
    - Test `get_uncovered_reasons()` defaults to threshold=100
    - Test `get_uncovered_reasons()` returns empty when all reasons covered
    - _Requirements: 1.4, 2.1_

- [x] 2. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Integrate post-test event analysis into controller and anomaly detector
  - [x] 3.1 Add EVENT_ANALYSIS log distinction in `src/k8s_scale_test/anomaly.py`
    - At the top of `handle_alert`, log "Post-test event analysis: ..." when `alert.alert_type == AlertType.EVENT_ANALYSIS`
    - _Requirements: 2.2, 2.3_
  - [x] 3.2 Add post-test event analysis block in `src/k8s_scale_test/controller.py` Phase 7a
    - Insert after health sweep results collected, before the `finally` block
    - Call `watcher.get_uncovered_reasons(self._findings, threshold=100)`
    - Loop over `uncovered[:3]`, create EVENT_ANALYSIS Alert for each, call `anomaly.handle_alert`, append Finding
    - Wrap each investigation in try/except, log errors and continue
    - Ensure this runs even when `hold_at_peak=0`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_
  - [x]* 3.3 Write unit tests for controller event analysis integration
    - Test cap at 3 investigations
    - Test error handling continues on individual failure
    - Test EVENT_ANALYSIS log message appears
    - _Requirements: 3.3, 3.8, 2.2_

- [x] 4. Final checkpoint - Ensure all tests pass
  - Run `python3 -m pytest tests/ -v` and ensure zero failures across all existing and new tests.
  - _Requirements: 5.1, 5.2_

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- No chart changes needed — existing cross-referencing logic automatically picks up EVENT_ANALYSIS findings
- The project uses `hypothesis` for property-based testing and `pytest` as the test runner
- Property tests should run minimum 100 iterations each
