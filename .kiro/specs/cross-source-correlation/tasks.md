# Implementation Plan: Cross-Source Correlation via Shared Context

## Overview

Implement a shared in-memory context that bridges the ObservabilityScanner and AnomalyDetector, enabling the anomaly detector to reference existing scanner findings instead of re-investigating known causes. The implementation follows the existing dependency injection pattern used for `aws_client`, `kb_store`, and `amp_collector`.

## Tasks

- [x] 1. Add correlation_window_seconds to TestConfig
  - Add `correlation_window_seconds: float = 120.0` field to TestConfig in `src/k8s_scale_test/models.py`
  - Place it after the existing `kb_auto_ingest` field
  - _Requirements: 3.2, 4.1_

- [x] 2. Implement SharedContext class and match_findings function
  - [x] 2.1 Create `src/k8s_scale_test/shared_context.py` with SharedContext class
    - Implement `__init__` with `max_age_seconds` parameter, `_entries` list, `_lock` asyncio.Lock
    - Implement synchronous `add(result, timestamp=None)` method that appends and calls `_evict()`
    - Implement async `query(reference_time, window_seconds=120.0)` method with asyncio.Lock
    - Implement `_evict()` to remove entries older than `max_age` from current time
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [x] 2.2 Implement `match_findings` function in `shared_context.py`
    - Extract node names from ScanResult title/detail using regex pattern for `ip-\d+-\d+-\d+-\d+\.\w+\.internal`
    - Classify matches as "strong" (temporal + resource overlap) or "weak" (temporal only)
    - Sort results by severity (CRITICAL first) then timestamp (most recent first)
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

  - [ ]* 2.3 Write property tests for SharedContext (Properties 1-4)
    - **Property 1: Add-then-query round trip**
    - **Validates: Requirements 1.1, 1.3, 2.2**
    - **Property 2: Query window filtering**
    - **Validates: Requirements 1.4, 3.2**
    - **Property 3: Eviction by max age**
    - **Validates: Requirements 1.5**
    - **Property 4: Concurrent read/write safety**
    - **Validates: Requirements 1.2, 8.3**

  - [ ]* 2.4 Write property tests for match_findings (Properties 5-6)
    - **Property 5: Matching correctness — strong vs weak vs excluded**
    - **Validates: Requirements 4.1, 4.2, 4.3**
    - **Property 6: Matching determinism**
    - **Validates: Requirements 4.4**

- [x] 3. Checkpoint - Ensure SharedContext and matching tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Integrate SharedContext into AnomalyDetector
  - [x] 4.1 Modify AnomalyDetector.__init__ to accept `shared_ctx` parameter
    - Add `shared_ctx=None` parameter after `kb_matcher` (or after `amp_collector` if Feature 2 is implemented)
    - Store as `self.shared_ctx`
    - _Requirements: 3.1, 3.3_

  - [x] 4.2 Modify AnomalyDetector.handle_alert to query SharedContext
    - Add Layer 0: query SharedContext before K8s events using `await self.shared_ctx.query()`
    - After Layer 4 (stuck nodes), perform resource matching using `match_findings()`
    - Wrap in `with span("investigation/shared_context")`
    - Pass `scanner_matches` to `_correlate()`
    - Handle SharedContext query failures gracefully (log, continue with empty matches)
    - _Requirements: 3.1, 3.2, 4.1, 4.2, 4.3, 7.2_

  - [x] 4.3 Modify AnomalyDetector._correlate to include scanner evidence
    - Add `scanner_matches=None` parameter
    - Add "scanner_correlation:" summary and "scanner:" per-match entries to evidence_references
    - _Requirements: 5.1_

  - [x] 4.4 Modify AnomalyDetector._extract_root_cause to include scanner clues
    - Add `scanner_matches=None` parameter
    - Add "Scanner pre-detected:" clues for WARNING/CRITICAL matches
    - _Requirements: 5.2_

  - [ ]* 4.5 Write property tests for anomaly detector integration (Properties 7-9)
    - **Property 7: Scanner evidence in evidence_references**
    - **Validates: Requirements 5.1**
    - **Property 8: Scanner clues in root cause for WARNING/CRITICAL**
    - **Validates: Requirements 5.2**
    - **Property 9: Valid Finding regardless of SharedContext state**
    - **Validates: Requirements 7.2, 8.5**

- [x] 5. Checkpoint - Ensure anomaly detector integration tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Wire SharedContext in Controller
  - [x] 6.1 Modify Controller to create and pass SharedContext
    - Add `self._shared_ctx: SharedContext | None = None` to `__init__`
    - In `run()`, after scanner creation, create SharedContext with `max_age_seconds=config.event_time_window_minutes * 60`
    - Pass `shared_ctx=self._shared_ctx` to AnomalyDetector constructor
    - _Requirements: 6.1, 6.2, 6.3_

  - [x] 6.2 Modify Controller._on_scanner_finding to write to SharedContext
    - Add `self._shared_ctx.add(result)` call with try/except for graceful degradation
    - _Requirements: 2.1, 7.1_

  - [ ]* 6.3 Write unit tests for controller wiring
    - Test SharedContext created when scanner exists
    - Test SharedContext is None when scanner not created
    - Test _on_scanner_finding writes to SharedContext
    - Test _on_scanner_finding handles SharedContext.add() failure gracefully
    - _Requirements: 6.1, 6.2, 6.3, 7.1_

- [x] 7. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- This feature depends on Feature 2 (AMP Reactive Investigation) for the `amp_collector` parameter ordering, but works independently if Feature 2 is not yet implemented
- Property tests validate universal correctness properties; unit tests validate specific examples and edge cases
- The SharedContext is intentionally not passed to the scanner directly — the controller's callback handles the write, keeping the scanner decoupled
