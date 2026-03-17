# Implementation Plan: Wire Observability Scanner

## Overview

Wire the existing `ObservabilityScanner` into `ScaleTestController` by adding executor factory functions, lifecycle management, context updates, finding collection, evidence persistence, and summary inclusion. All changes are in `controller.py`, `evidence.py`, and `models.py` — no modifications to `monitor.py`, `health_sweep.py`, `anomaly.py`, `infra_health.py`, or `events.py`.

## Tasks

- [x] 1. Add executor factory functions and scanner creation logic
  - [x] 1.1 Create `_make_prometheus_executor(config, aws_profile)` function in `controller.py`
    - Returns an async callable wrapping `AMPMetricCollector._query_promql` when `config.amp_workspace_id` or `config.prometheus_url` is set
    - Returns `None` when neither is set
    - Import `AMPMetricCollector` from `health_sweep`
    - _Requirements: 1.2, 1.3_
  - [x] 1.2 Create `_make_cloudwatch_executor(config, aws_client)` function in `controller.py`
    - Returns an async callable wrapping CloudWatch Logs Insights via boto3 when `config.cloudwatch_log_group` and `config.eks_cluster_name` are both set
    - Returns `None` when either is missing
    - _Requirements: 1.4, 1.5_
  - [ ]* 1.3 Write property test for config-to-executor mapping
    - **Property 1: Config-to-executor mapping determines scanner creation**
    - Generate random `TestConfig` instances with optional AMP/Prometheus/CW fields
    - Verify executor nullity matches config field presence
    - Verify scanner creation decision (created iff at least one executor non-null)
    - **Validates: Requirements 1.2, 1.3, 1.4, 1.5, 1.6**

- [x] 2. Wire scanner into controller lifecycle
  - [x] 2.1 Add scanner attributes to `ScaleTestController.__init__`
    - Add `self._scanner: ObservabilityScanner | None = None`
    - Add `self._scanner_task: asyncio.Task | None = None`
    - Add `self._scanner_findings: list[ScanResult] = []`
    - _Requirements: 5.4_
  - [x] 2.2 Create scanner in `run()` after monitoring setup (Phase 5), before scaling (Phase 7)
    - Call `_make_prometheus_executor` and `_make_cloudwatch_executor`
    - If both executors are None, log skip message and leave `self._scanner` as None
    - Otherwise create `ObservabilityScanner(config, prom_fn, cw_fn)`, register finding callback, start as asyncio task
    - _Requirements: 1.1, 1.6, 2.1, 5.1_
  - [x] 2.3 Add `scanner.stop()` and task await to the finally block
    - Stop scanner and await task with exception handling, alongside existing monitor/watcher/observer cleanup
    - _Requirements: 2.2, 2.3, 8.3_
  - [ ]* 2.4 Write property test for scanner stop causes task completion
    - **Property 2: Scanner stop causes task completion**
    - Create scanner with mock executors, start run(), call stop(), verify task completes
    - **Validates: Requirements 2.2**

- [x] 3. Add phase transitions and context updates
  - [x] 3.1 Call `scanner.set_phase(Phase.SCALING)` before `_execute_scaling_via_flux`
    - Guard with `if self._scanner:` check
    - _Requirements: 3.1_
  - [x] 3.2 Call `scanner.set_phase(Phase.HOLD_AT_PEAK)` before the hold-at-peak period
    - Guard with `if self._scanner:` check
    - _Requirements: 3.2_
  - [x] 3.3 Call `scanner.update_context()` in the scaling loop after `_count_pods()`
    - Pass `elapsed_minutes=elapsed/60`, `pending=pending`, `ready=ready`
    - Guard with `if self._scanner:` check
    - _Requirements: 4.1, 4.2, 4.3, 4.4_
  - [ ]* 3.4 Write property test for context update fidelity
    - **Property 3: Context update fidelity**
    - Generate random floats/ints, call update_context, verify scanner context dict matches
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.4**

- [x] 4. Implement finding callback and evidence persistence
  - [x] 4.1 Add `_on_scanner_finding(self, result: ScanResult)` method to `ScaleTestController`
    - Log at warning level with query_name, severity, title
    - Persist to evidence store via `save_scanner_finding`
    - Append to `self._scanner_findings`
    - Wrap persistence in try/except
    - _Requirements: 5.2, 5.3, 5.4, 5.5_
  - [x] 4.2 Add `save_scanner_finding(run_id, result)` method to `EvidenceStore`
    - Serialize ScanResult to JSON and append to `scanner_findings.jsonl` in the run directory
    - _Requirements: 5.3_
  - [ ]* 4.3 Write property test for finding callback collects and persists
    - **Property 4: Finding callback collects and persists**
    - Generate random ScanResult instances, fire callback, verify collection in `_scanner_findings` and persistence in evidence store
    - **Validates: Requirements 5.2, 5.3, 5.4**

- [x] 5. Include scanner findings in test run summary
  - [x] 5.1 Add `scanner_findings: Optional[List[Dict]] = None` field to `TestRunSummary` in `models.py`
    - _Requirements: 6.1, 6.2_
  - [x] 5.2 Update `_make_summary` to serialize and include `self._scanner_findings` in the summary
    - Serialize each ScanResult to a dict with query_name, severity, title, detail, source
    - _Requirements: 6.1, 6.2_
  - [ ]* 5.3 Write property test for summary includes scanner findings separately
    - **Property 5: Summary includes scanner findings separately**
    - Generate random lists of ScanResult and Finding, build summary, verify scanner_findings and findings are separate
    - **Validates: Requirements 6.1, 6.2**

- [x] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- All changes are in `controller.py`, `evidence.py` (or equivalent evidence store module), and `models.py`
- No modifications to `monitor.py`, `health_sweep.py`, `anomaly.py`, `infra_health.py`, or `events.py`
- Property tests use `pytest` + `hypothesis` with `@settings(max_examples=100)`
- Each property test is tagged: `Feature: wire-observability-scanner, Property N: <title>`
