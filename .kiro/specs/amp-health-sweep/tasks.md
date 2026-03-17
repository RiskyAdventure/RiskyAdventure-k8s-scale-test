# Implementation Plan: AMP Health Sweep

## Overview

Replace the SSM-based health sweep with AMP PromQL queries and K8s API node conditions. The implementation proceeds bottom-up: internal data types first, then the individual collectors (AMP, K8s conditions, SSM fallback), then the orchestrating HealthSweepAgent, and finally controller integration.

## Tasks

- [ ] 1. Create internal data types and threshold logic
  - [ ] 1.1 Add `NodeMetricResult` and `NodeConditionResult` dataclasses to `health_sweep.py`
    - `NodeMetricResult(node_name, metric_category, value)`
    - `NodeConditionResult(node_name, issues)`
    - _Requirements: 4.1_
  - [ ] 1.2 Implement `check_threshold(result: NodeMetricResult) -> str | None` function
    - Returns issue string if value exceeds category threshold, None otherwise
    - Thresholds: CPU > 90%, memory > 90%, disk > 85%, network_errors > 0/s, pod_restarts > 5
    - _Requirements: 4.2, 4.3, 4.4, 4.5, 4.6_
  - [ ]* 1.3 Write property test for threshold violation detection
    - **Property 3: Threshold violation detection**
    - **Validates: Requirements 4.2, 4.3, 4.4, 4.5, 4.6**
  - [ ] 1.4 Implement `parse_promql_response(response: dict, category: str) -> list[NodeMetricResult]` function
    - Parses Prometheus vector response JSON into NodeMetricResult list
    - Extracts `metric.node` (or `metric.instance`) and `value[1]` as float
    - Returns empty list on malformed input
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_
  - [ ]* 1.5 Write property test for PromQL response parsing
    - **Property 5: PromQL response parsing**
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5**

- [ ] 2. Implement K8sConditionChecker
  - [ ] 2.1 Create `K8sConditionChecker` class in `health_sweep.py`
    - `__init__(self, k8s_client)` — stores k8s client
    - `async check_all(self) -> list[NodeConditionResult]` — single `list_node` call, classifies conditions
    - Flags: Ready != "True", DiskPressure/MemoryPressure/PIDPressure == "True"
    - Returns empty list on K8s API failure (log error, don't raise)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 5.3, 6.2_
  - [ ]* 2.2 Write property test for node condition classification
    - **Property 1: Node condition classification**
    - **Validates: Requirements 2.2, 2.3, 2.4, 2.5**

- [ ] 3. Implement AMPMetricCollector
  - [ ] 3.1 Create `AMPMetricCollector` class in `health_sweep.py`
    - `__init__(self, amp_workspace_id, prometheus_url, aws_profile, region)` — resolves endpoint URL and auth strategy
    - `async collect_all(self) -> dict[str, list[NodeMetricResult]]` — runs all 5 PromQL queries concurrently via `asyncio.gather`
    - `async _query_promql(self, query: str) -> dict` — HTTP GET with SigV4 (AMP) or plain (Prometheus URL)
    - Uses `botocore.auth.SigV4Auth` for AMP signing, `urllib.request` or `aiohttp` for HTTP
    - Each failed query logs warning and returns empty list for that category
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 5.2, 6.1_

- [ ] 4. Implement SSMFallbackCollector
  - [ ] 4.1 Extract existing SSM sweep logic from `HealthSweepAgent.run()` into `SSMFallbackCollector` class
    - `__init__(self, k8s_client, node_diag, extra_commands=None)`
    - `async collect(self, sample_size=10) -> dict` — returns partial Sweep_Result dict
    - Reuses `parse_sweep_output` for standard health check output
    - Runs extra_commands via SSM alongside the standard command, stores output in `extra_diagnostics` per node
    - _Requirements: 3.1, 3.3, 3.4, 3.5_

- [ ] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 6. Refactor HealthSweepAgent to orchestrate collectors
  - [ ] 6.1 Refactor `HealthSweepAgent.__init__` to accept `TestConfig` and create collectors based on config
    - Accept `config: TestConfig` instead of bare `k8s_client`
    - Store config, k8s_client, node_diag, evidence_store, run_id
    - _Requirements: 3.1, 3.2_
  - [ ] 6.2 Implement strategy selection in `HealthSweepAgent.run()`
    - If `config.amp_workspace_id` or `config.prometheus_url` → use AMPMetricCollector + K8sConditionChecker
    - Else → use SSMFallbackCollector
    - _Requirements: 3.1, 3.2_
  - [ ]* 6.3 Write property test for strategy selection
    - **Property 2: Strategy selection by config**
    - **Validates: Requirements 3.1, 3.2**
  - [ ] 6.4 Implement `_merge_results` method
    - Combines AMP metrics (with threshold checks) and K8s conditions into Sweep_Result dict
    - Sets `nodes_sampled`, `healthy`, `issues`, `node_details`
    - Includes `raw_metrics` key when AMP is used
    - _Requirements: 4.1, 4.7, 4.8, 4.9, 4.10, 7.2_
  - [ ]* 6.5 Write property test for result structure and count invariants
    - **Property 4: Result structure and count invariants**
    - **Validates: Requirements 4.1, 4.8, 4.9, 4.10**
  - [ ]* 6.6 Write property test for raw metrics inclusion
    - **Property 6: Raw metrics inclusion when AMP is used**
    - **Validates: Requirements 7.2**
  - [ ] 6.7 Ensure evidence persistence in all code paths
    - Write Sweep_Result to `diagnostics/health_sweep.json` via evidence store
    - Persist even on partial failures
    - _Requirements: 6.3, 6.4, 7.1_

- [ ] 7. Update controller integration
  - [ ] 7.1 Update `ScaleTestController.run()` to pass `TestConfig` to the refactored `HealthSweepAgent`
    - Update the `HealthSweepAgent` constructor call in the controller
    - Ensure the sweep agent receives config for strategy selection
    - _Requirements: 3.1, 3.2_
  - [ ]* 7.2 Write unit tests for controller integration
    - Test that HealthSweepAgent is constructed with correct config
    - Test that sweep result format is consumed correctly by downstream code
    - _Requirements: 4.1_

- [ ] 8. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Property tests use `hypothesis` (already in the project)
- The existing `parse_sweep_output` function and its tests are preserved unchanged
- The controller's downstream pipeline (summary, chart, operator prompt) requires no changes due to format compatibility
