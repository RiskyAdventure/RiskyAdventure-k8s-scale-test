# Implementation Plan: AWS Observability Integration

## Overview

Incremental implementation of three observability clients, two analysis agents, a post-run reporter, and integration into the existing controller/anomaly detector. Each task builds on the previous, with tests close to implementation. The approach: data models first, then clients, then agents, then integration wiring.

## Tasks

- [ ] 1. Add configuration fields and CLI arguments
  - [ ] 1.1 Add `amp_endpoint`, `cloudwatch_log_group`, and `eks_cluster_name` optional fields to `TestConfig` in `models.py`, defaulting to `None`
    - _Requirements: 9.1_
  - [ ] 1.2 Add `--amp-endpoint`, `--cloudwatch-log-group`, and `--eks-cluster-name` arguments to `parse_args()` in `cli.py`, wiring them into `TestConfig`
    - _Requirements: 9.2, 9.3, 9.4, 9.5_

- [ ] 2. Implement AMP Client
  - [ ] 2.1 Create `src/k8s_scale_test/amp_client.py` with `AMPTimeSeries`, `AMPQueryResult` dataclasses (extending `_SerializableMixin`) and `AMPClient` class
    - Implement `__init__` accepting optional workspace URL and AWS session
    - Implement `is_available()` returning False when URL is None
    - Implement `query_range()` and `query_instant()` using SigV4-signed HTTP requests via `loop.run_in_executor`
    - Implement pre-built query methods: `get_node_cpu_utilization`, `get_node_memory_utilization`, `get_node_network_bytes`, `get_node_disk_io_utilization`, `get_pod_cpu_by_namespace`, `get_pod_memory_by_namespace`, `get_pod_restarts_by_namespace`
    - All methods return `AMPQueryResult` with appropriate status, never raise exceptions
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_
  - [ ]* 2.2 Write property test: AMP query results are well-structured
    - **Property 1: AMP query results are well-structured**
    - **Validates: Requirements 1.1, 1.2**
  - [ ]* 2.3 Write property test: Observability client errors never propagate as exceptions (AMP portion)
    - **Property 2: Observability client errors never propagate as exceptions**
    - **Validates: Requirements 1.3**
  - [ ]* 2.4 Write property test: AMP requests include SigV4 authentication
    - **Property 3: AMP requests include SigV4 authentication**
    - **Validates: Requirements 1.6**
  - [ ]* 2.5 Write property test: Unconfigured clients return unavailable status (AMP portion)
    - **Property 4: Unconfigured clients return unavailable status**
    - **Validates: Requirements 1.7**

- [ ] 3. Implement CloudWatch Logs Client
  - [ ] 3.1 Create `src/k8s_scale_test/cloudwatch_client.py` with `CWLQueryResult` dataclass (extending `_SerializableMixin`) and `CloudWatchLogsClient` class
    - Implement `__init__` accepting optional log group, AWS session, and query timeout
    - Implement `is_available()` returning False when log group is None
    - Implement `run_insights_query()` with StartQuery, exponential backoff polling (0.5s base, 5s max), timeout cancellation via StopQuery
    - Implement pre-built queries: `get_kubelet_errors`, `get_containerd_errors`, `get_ipamd_exhaustion`, `get_oom_events`, `get_node_errors`
    - All boto3 calls via `loop.run_in_executor`, all methods return `CWLQueryResult`, never raise exceptions
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_
  - [ ]* 3.2 Write property test: CloudWatch Logs polling uses exponential backoff
    - **Property 8: CloudWatch Logs polling uses exponential backoff**
    - **Validates: Requirements 2.7**
  - [ ]* 3.3 Write property test: CloudWatch Logs query timeout triggers cancellation
    - **Property 6: CloudWatch Logs query timeout triggers cancellation**
    - **Validates: Requirements 2.3**
  - [ ]* 3.4 Write property test: CloudWatch Logs node-specific queries filter by hostname
    - **Property 7: CloudWatch Logs node-specific queries filter by hostname**
    - **Validates: Requirements 2.5**
  - [ ]* 3.5 Write property test: Unconfigured clients return unavailable status (CWL portion)
    - **Property 4: Unconfigured clients return unavailable status**
    - **Validates: Requirements 2.6**

- [ ] 4. Implement EKS API Client
  - [ ] 4.1 Create `src/k8s_scale_test/eks_client.py` with `EKSAddonInfo`, `EKSClusterInfo` dataclasses (extending `_SerializableMixin`) and `EKSAPIClient` class
    - Implement `__init__` accepting optional cluster name and AWS session
    - Implement `is_available()` returning False when cluster name is None
    - Implement `get_cluster_info()` calling DescribeCluster + ListAddons + DescribeAddon per addon, all via `loop.run_in_executor`
    - Return `EKSClusterInfo` with error_message on failure, never raise exceptions
    - _Requirements: 3.1, 3.2, 3.3, 3.4_
  - [ ]* 4.2 Write property test: EKS cluster info contains all required fields
    - **Property 9: EKS cluster info contains all required fields**
    - **Validates: Requirements 3.1, 3.2**
  - [ ]* 4.3 Write property test: Observability client errors never propagate as exceptions (EKS portion)
    - **Property 2: Observability client errors never propagate as exceptions**
    - **Validates: Requirements 3.3**

- [ ] 5. Checkpoint - Ensure all client tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 6. Implement Metrics Analyzer Agent
  - [ ] 6.1 Create `src/k8s_scale_test/metrics_agent.py` with `NodeResourceSummary`, `MetricsAnalysisSummary` dataclasses (extending `_SerializableMixin`) and `MetricsAnalyzerAgent` class
    - Implement `__init__` accepting `AMPClient` and `EvidenceStore`
    - Implement `run()` that queries AMP for CPU, memory, network, disk metrics, computes fleet averages, identifies threshold violations (CPU > 80%, memory > 85%), ranks by severity, limits to top 10, persists summary to evidence store as `metrics_analysis.json`
    - Return `MetricsAnalysisSummary` with status "unavailable" when AMP is not configured
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.6_
  - [ ]* 6.2 Write property test: Metrics agent correctly identifies threshold violations and ranks by severity
    - **Property 10: Metrics agent correctly identifies threshold violations and ranks by severity**
    - **Validates: Requirements 4.2, 4.3**
  - [ ]* 6.3 Write property test: Analysis summary serialization round-trip (MetricsAnalysisSummary)
    - **Property 16: Analysis summary serialization round-trip**
    - **Validates: Requirements 4.6**

- [ ] 7. Implement Log Analyzer Agent
  - [ ] 7.1 Create `src/k8s_scale_test/log_agent.py` with `LogErrorGroup`, `LogAnalysisSummary` dataclasses (extending `_SerializableMixin`) and `LogAnalyzerAgent` class
    - Implement `__init__` accepting `CloudWatchLogsClient` and `EvidenceStore`
    - Implement `run()` that queries CWL for kubelet errors, containerd errors, IPAMD exhaustion, OOM events, groups by source, ranks by frequency, detects IPAMD exhaustion nodes, persists summary to evidence store as `log_analysis.json`
    - Return `LogAnalysisSummary` with status "unavailable" when CWL is not configured
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_
  - [ ]* 7.2 Write property test: Log agent correctly groups errors by source
    - **Property 11: Log agent correctly groups errors by source**
    - **Validates: Requirements 5.2**
  - [ ]* 7.3 Write property test: Log agent detects IPAMD exhaustion nodes
    - **Property 12: Log agent detects IPAMD exhaustion nodes**
    - **Validates: Requirements 5.3**

- [ ] 8. Implement Post-Run Reporter
  - [ ] 8.1 Create `src/k8s_scale_test/post_run_reporter.py` with `ObservabilityReport` dataclass (extending `_SerializableMixin`) and `PostRunReporter` class
    - Implement `__init__` accepting all three clients and `EvidenceStore`
    - Implement `generate()` that runs MetricsAnalyzerAgent, LogAnalyzerAgent, and EKS cluster info query concurrently via `asyncio.gather`, correlates with existing findings, persists report as `observability_report.json`
    - Handle partial availability: classify sources as available/unavailable, set status to "complete" or "partial"
    - _Requirements: 6.1, 6.2, 6.3, 6.4_
  - [ ]* 8.2 Write property test: Post-run reporter handles partial source availability
    - **Property 13: Post-run reporter handles partial source availability**
    - **Validates: Requirements 6.2, 6.3**

- [ ] 9. Checkpoint - Ensure all agent tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 10. Integrate observability into Anomaly Detector
  - [ ] 10.1 Modify `AnomalyDetector.__init__` in `anomaly.py` to accept optional `amp_client` and `cwl_client` parameters
    - _Requirements: 7.4, 8.4_
  - [ ] 10.2 Add Layer 7 (AMP metrics) and Layer 8 (CloudWatch Logs) to `handle_alert()` in `anomaly.py`
    - Query AMP for CPU/memory metrics on stuck pod nodes (from `targets` list)
    - Query CWL for recent errors on stuck pod nodes
    - Run new queries concurrently with existing evidence collection via `asyncio.gather`
    - Add "amp:" and "cwl:" prefixed entries to `evidence_references`
    - Wrap each new layer in try/except, log failures, continue with remaining layers
    - _Requirements: 7.1, 7.2, 7.3_
  - [ ]* 10.3 Write property test: Anomaly detector includes observability evidence when configured
    - **Property 14: Anomaly detector includes observability evidence when configured**
    - **Validates: Requirements 7.1, 7.2**
  - [ ]* 10.4 Write property test: Anomaly detector produces valid findings despite observability failures
    - **Property 15: Anomaly detector produces valid findings despite observability failures**
    - **Validates: Requirements 7.3, 7.4**

- [ ] 11. Integrate observability into Controller lifecycle
  - [ ] 11.1 Modify `ScaleTestController.__init__` and `run()` in `controller.py` to create observability clients from TestConfig
    - Create `AMPClient`, `CloudWatchLogsClient`, `EKSAPIClient` during monitoring setup (Phase 5)
    - Pass `amp_client` and `cwl_client` to `AnomalyDetector` constructor
    - Wrap client creation in try/except, log warning and set to None on failure
    - _Requirements: 8.1, 8.4, 8.5_
  - [ ] 11.2 Add Metrics_Analyzer_Agent and Log_Analyzer_Agent to hold-at-peak phase in `controller.py`
    - Create `MetricsAnalyzerAgent` and `LogAnalyzerAgent` instances
    - Run them concurrently with `HealthSweepAgent` via `asyncio.gather` during hold-at-peak
    - Store results for post-run reporter
    - _Requirements: 8.2_
  - [ ] 11.3 Add Post-Run Reporter invocation after cleanup in `controller.py`
    - Create `PostRunReporter` and call `generate()` after `_cleanup_pods` and before chart generation
    - Pass existing findings list for correlation
    - _Requirements: 8.3_

- [ ] 12. Enhance chart with AMP metrics
  - [ ] 12.1 Modify `generate_chart()` in `chart.py` to load `metrics_analysis.json` from the run directory and render fleet CPU/memory utilization as additional Chart.js time-series plots
    - Check if `metrics_analysis.json` exists; if not, skip the section
    - Add a new canvas element and Chart.js scatter plot for CPU and memory utilization over time, aligned to the same time axis
    - _Requirements: 11.1, 11.2, 11.3_
  - [ ]* 12.2 Write property test: Chart includes resource utilization section when AMP data exists
    - **Property 17: Chart includes resource utilization section when AMP data exists**
    - **Validates: Requirements 11.1**

- [ ] 13. Add Evidence Store persistence methods
  - [ ] 13.1 Add `save_metrics_analysis`, `save_log_analysis`, and `save_observability_report` methods to `EvidenceStore` in `evidence.py`
    - Follow existing pattern: accept run_id and dataclass, write JSON to run directory
    - Add corresponding `load_` methods for post-run access
    - _Requirements: 4.6, 5.5, 6.4_

- [ ] 14. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties
- Unit tests validate specific examples and edge cases
- All new modules follow existing patterns: async with `run_in_executor`, `_SerializableMixin` for dataclasses, graceful degradation when unconfigured
