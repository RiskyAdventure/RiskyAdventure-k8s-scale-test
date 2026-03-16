# Implementation Plan: ClusterLoader2 Preload Integration

## Overview

Incremental implementation starting with data models and parser (testable in isolation), then Flux manifests, CLI changes, controller preload method, evidence store, and chart. Each step builds on the previous. CL2 preload is added as methods on the existing `ScaleTestController` — no separate controller class.

## Tasks

- [-] 1. Add CL2 data models to models.py
  - [x] 1.1 Add CL2 dataclasses and update TestConfig
    - Add `LatencyPercentile`, `APILatencyResult`, `SchedulingThroughputResult`, `CL2TestStatus`, `CL2Summary` dataclasses extending `_SerializableMixin`
    - Add `CL2ParseError` exception
    - Add `cl2_preload`, `cl2_timeout`, `cl2_params` optional fields to `TestConfig`
    - _Requirements: 3.5, 5.1, 5.6_
  - [ ]* 1.2 Write property test for CL2Summary serialization round-trip
    - **Property 5: CL2Summary serialization round-trip**
    - Create Hypothesis strategies for generating random `CL2Summary` objects in `tests/conftest.py`
    - Verify `CL2Summary.from_dict(summary.to_dict())` equals the original
    - **Validates: Requirements 5.6**
  - [ ]* 1.3 Write property test for Evidence store backward compatibility
    - **Property 8: Evidence store backward compatibility round-trip**
    - Generate random `TestRunSummary` objects (existing fields only)
    - Verify `TestRunSummary.from_dict(summary.to_dict())` equals the original
    - **Validates: Requirements 7.2, 7.3**

- [-] 2. Implement CL2 result parser
  - [ ] 2.1 Create `src/k8s_scale_test/cl2_parser.py` with `CL2ResultParser` class
    - Implement `parse(raw_json)`, `extract_pod_startup_latency()`, `extract_api_responsiveness()`, `extract_scheduling_throughput()`
    - Handle malformed JSON by raising `CL2ParseError` with descriptive message
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.7_
  - [ ]* 2.2 Write property test for CL2 metric extraction completeness
    - **Property 4: CL2 metric extraction completeness**
    - Generate random valid CL2 JSON with varying dataItems and metric types
    - Verify all present metrics are correctly extracted into the CL2Summary
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.4**
  - [ ]* 2.3 Write property test for malformed CL2 JSON error handling
    - **Property 6: Malformed CL2 JSON produces CL2ParseError**
    - Generate random non-JSON strings and JSON without `dataItems`
    - Verify `CL2ParseError` is raised with non-empty message
    - **Validates: Requirements 5.7**
  - [ ]* 2.4 Write unit tests for CL2 parser with real CL2 output fixtures
    - Test with example CL2 JSON matching the documented output structure
    - Test edge cases: empty dataItems, missing optional fields, zero values
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.7_

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Create Flux manifests for CL2 deployment
  - [x] 4.1 Create `flux2/apps/base/clusterloader2/namespace.yaml` and `serviceaccount.yaml`
    - Namespace: `scale-test`, ServiceAccount: `clusterloader2`
    - _Requirements: 1.1, 1.2_
  - [x] 4.2 Create `flux2/apps/base/clusterloader2/rbac.yaml`
    - ClusterRole with permissions for core, apps, batch API groups
    - ClusterRoleBinding to `clusterloader2` ServiceAccount
    - _Requirements: 1.5, 1.6_
  - [x] 4.3 Create `flux2/apps/base/clusterloader2/job.yaml` and `configmap.yaml`
    - Job with CL2 image, ConfigMap mount, emptyDir, `restartPolicy: Never`, `backoffLimit: 0`
    - _Requirements: 1.2, 1.3, 1.4_
  - [x] 4.4 Create `flux2/apps/base/clusterloader2/kustomization.yaml`
    - Reference all manifests
    - _Requirements: 1.1_

- [ ] 5. Create CL2 test config templates
  - [ ] 5.1 Create `flux2/apps/base/clusterloader2/configs/mixed-workload.yaml`
    - Parameterized deployments, services, configmaps, secrets, daemonsets with nodeSelector `karpenter: alt`
    - Measurement collectors for all SLI metrics
    - _Requirements: 2.1, 2.2, 2.5, 2.6, 1.7_
  - [ ] 5.2 Create `flux2/apps/base/clusterloader2/configs/service-mesh-stress.yaml`
    - Parameterized services with endpoints, DNS and endpoint propagation measurements
    - _Requirements: 2.1, 2.3, 2.5, 2.6_
  - [x] 5.3 Create `flux2/apps/base/clusterloader2/configs/configmap-secret-churn.yaml`
    - ConfigMap/Secret create and update at configurable rate, API latency measurement
    - _Requirements: 2.1, 2.4, 2.5, 2.6_

- [ ] 6. Extend CLI with --cl2-preload flag
  - [x] 6.1 Update `src/k8s_scale_test/cli.py` with `--cl2-preload`, `--cl2-timeout`, `--cl2-params` arguments
    - Parse `--cl2-params` from `KEY=VAL,KEY=VAL` string into dict
    - Pass CL2 fields into `TestConfig` in `main()`
    - No branching — controller handles preload internally
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 7.1_
  - [ ]* 6.2 Write property tests for CLI defaults and CL2 argument parsing
    - **Property 1: CLI defaults preserve existing behavior**
    - **Property 2: CLI CL2 argument parsing**
    - Verify cl2_preload is None when flag absent, matches input when present
    - **Validates: Requirements 3.1, 3.2, 3.3, 7.1**
  - [ ]* 6.3 Write property test for CL2 params parsing
    - **Property 3: CL2 params parsing**
    - Generate random KEY=VAL pairs, verify parsing produces correct dict
    - **Validates: Requirements 3.4**

- [x] 7. Implement CL2 preload and cleanup on ScaleTestController
  - [x] 7.1 Add `_run_cl2_preload()` and helper methods to `ScaleTestController`
    - `_read_cl2_config_template()`, `_inject_cl2_config()`, `_update_cl2_job_name()`
    - `_wait_for_cl2_job()` with 10s polling and token refresh on 401
    - `_collect_cl2_results()` to retrieve pod logs
    - Full lifecycle orchestration: template -> inject -> job name -> git push -> wait -> collect -> parse -> save
    - On failure/timeout: save partial results, prompt operator
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.8_
  - [x] 7.2 Add `_cleanup_cl2()` method to `ScaleTestController`
    - Delete CL2-created namespaces (prefixed `cl2-test-`) after pod scaling cleanup
    - Non-fatal: log errors but don't fail the run
    - _Requirements: 4.7_
  - [x] 7.3 Wire CL2 preload into `ScaleTestController.run()` flow
    - Insert `_run_cl2_preload()` after operator approval, before deployment discovery
    - Insert `_cleanup_cl2()` after `_cleanup_pods()`, before summary generation
    - Only execute when `self.config.cl2_preload is not None`
    - _Requirements: 4.1, 4.6, 4.7_

- [ ] 8. Update Evidence Store for CL2 data
  - [ ] 8.1 Add `save_cl2_summary()` and `load_cl2_summary()` to `EvidenceStore`
    - `save_cl2_summary()` writes `cl2_summary.json`
    - `load_cl2_summary()` returns None if file doesn't exist
    - Update `load_run()` to include `cl2_summary` key
    - _Requirements: 5.5, 7.2_

- [ ] 9. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 10. Update chart generation for CL2 metrics
  - [x] 10.1 Update `src/k8s_scale_test/chart.py` to render CL2 SLI metrics
    - Detect `cl2_summary.json`, render PodStartupLatency bar chart, APIResponsiveness table, SchedulingThroughput stat
    - If absent, render chart exactly as before
    - Keep HTML self-contained with inline Chart.js
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_
  - [x]* 10.2 Write property test for chart backward compatibility
    - **Property 7: Chart renders without CL2 sections when no CL2 data exists**
    - Verify HTML output does not contain CL2-specific strings when no cl2_summary.json
    - **Validates: Requirements 6.4**
  - [ ]* 10.3 Write unit tests for chart CL2 rendering
    - Test chart with and without CL2 summary fixture
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

- [ ] 11. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests use Hypothesis with minimum 100 examples per property
- Flux manifests (tasks 4-5) are static YAML — no automated tests needed
- CL2 preload methods reuse existing controller infrastructure (_git_commit_push, _refresh_k8s_token, _prompt_operator)
- No separate CL2Controller class — all preload logic is methods on ScaleTestController
