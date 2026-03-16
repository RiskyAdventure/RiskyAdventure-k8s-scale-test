# Implementation Plan: Stress-Test Integration

## Overview

Replace the pause/grow app with stress-test Deployments, add dynamic sizing in preflight, weighted distribution in the controller, and role-based deployment filtering. Implementation proceeds bottom-up: data models → manifest changes → preflight sizing → flux reader/writer → controller orchestration → CLI.

## Tasks

- [x] 1. Add new data models and extend existing ones
  - [x] 1.1 Add `StressorSizing` dataclass to `models.py`
    - Fields: `cpu_request_millicores`, `memory_request_mi`, `pod_ceiling`, `instance_type`, `allocatable_cpu_millicores`, `allocatable_memory_mi`, `daemonset_count`, `cpu_limit_multiplier`, `memory_limit_multiplier`
    - Extend `_SerializableMixin`
    - _Requirements: 2.1, 2.3_
  - [x] 1.2 Add `stressor_sizing: Optional[StressorSizing] = None` field to `PreflightReport`
    - _Requirements: 2.3_
  - [x] 1.3 Add `role: Optional[str] = None` and `weight: Optional[float] = None` fields to `DeploymentConfig`
    - _Requirements: 4.5, 10.1_
  - [x] 1.4 Add `stressor_weights`, `cpu_limit_multiplier`, `memory_limit_multiplier`, `iperf3_server_ratio` fields to `TestConfig`
    - _Requirements: 9.1, 2.7_

- [x] 2. Update Flux manifest YAML files
  - [x] 2.1 Rewrite `cpu-stress.yaml`: remove validator sidecar and hostPath volume, add `scale-test/role: stressor` label, add topology spread constraints, add rolling update strategy, set conservative default resources (10m CPU, 128Mi memory)
    - _Requirements: 1.1, 2.6, 5.1, 8.1, 8.2, 8.3, 8.4_
  - [x] 2.2 Rewrite `memory-stress.yaml`: remove zram-validator sidecar, add `scale-test/role: stressor` label, add topology spread constraints, add rolling update strategy, set conservative default resources
    - _Requirements: 2.6, 5.2, 8.1, 8.2, 8.3, 8.4_
  - [x] 2.3 Rewrite `network-stress.yaml`: remove network-validator from iperf3-server, add `scale-test/role: infrastructure` to iperf3-server, add `scale-test/role: stressor` to iperf3-client, add topology spread and rolling update to both, retain headless Service
    - _Requirements: 5.3, 6.4, 8.1, 8.2, 8.3, 8.4_
  - [x] 2.4 Rewrite `sysctl-stress.yaml`: replace privileged curl-based container with non-privileged stress-ng `--sock` workers, add `scale-test/role: stressor` label, add topology spread and rolling update
    - _Requirements: 5.4, 8.1, 8.2, 8.3, 8.4_
  - [x] 2.5 Create `io-stress.yaml`: new Deployment replacing the CronJob, continuous loop with stress-ng `--iomix`, add `scale-test/role: stressor` label, add topology spread and rolling update, conservative defaults
    - _Requirements: 3.1, 3.2, 3.3_
  - [x] 2.6 Delete `cronjob.yaml`
    - _Requirements: 3.1_
  - [x] 2.7 Update `kustomization.yaml`: remove `cronjob.yaml`, add `io-stress.yaml`, add `image-puller-daemonset.yaml`
    - _Requirements: 1.2, 7.4_
  - [x] 2.8 Update `image-puller-daemonset.yaml`: change init containers to pull `polinux/stress-ng:latest` and `networkstatic/iperf3:latest`
    - _Requirements: 7.1, 7.2, 7.3_
  - [x] 2.9 Update `flux2/apps/overlay-prod/kustomization.yaml`: replace `../base/pause` with `../base/stress-test`
    - _Requirements: 1.1_

- [x] 3. Checkpoint — Verify manifest changes
  - Ensure all YAML files are valid, ask the user if questions arise.

- [x] 4. Implement FluxRepoReader changes
  - [x] 4.1 Update `get_deployments()` in `flux.py` to read `scale-test/role` label from `metadata.labels` and populate `DeploymentConfig.role`
    - _Requirements: 10.1_
  - [ ]* 4.2 Write property test for FluxRepoReader role label discovery
    - **Property 9: FluxRepoReader reads scale-test/role label into DeploymentConfig**
    - **Validates: Requirements 10.1**
  - [ ]* 4.3 Write property test for FluxRepoReader deployment discovery completeness
    - **Property 10: FluxRepoReader discovers all Deployments in directory**
    - **Validates: Requirements 1.3**

- [x] 5. Implement FluxRepoWriter changes
  - [x] 5.1 Add `set_resource_requests()` method to `FluxRepoWriter` in `flux.py`
    - Accepts name, cpu_request, memory_request, cpu_limit, memory_limit, source_path
    - Updates first container's resources in the matching Deployment document
    - _Requirements: 2.5_
  - [x] 5.2 Add `distribute_pods_weighted()` method to `FluxRepoWriter` in `flux.py`
    - Accepts deployments, total_target, weights dict
    - Validates weights sum to 1.0 ± 0.01
    - Floor allocation + round-robin remainder from highest-weighted
    - _Requirements: 4.1, 4.2_
  - [ ]* 5.3 Write property test for set_resource_requests round-trip
    - **Property 4: set_resource_requests round-trip consistency**
    - **Validates: Requirements 2.4, 2.5**
  - [ ]* 5.4 Write property test for weighted distribution
    - **Property 5: Weighted distribution preserves total and respects proportions**
    - **Validates: Requirements 4.1, 4.2**
  - [ ]* 5.5 Write property test for invalid weights rejection
    - **Property 6: Invalid weights are rejected**
    - **Validates: Requirements 4.4**
  - [ ]* 5.6 Write property test for even distribution fallback
    - **Property 7: Even distribution fallback preserves total**
    - **Validates: Requirements 4.3**

- [x] 6. Checkpoint — Verify FluxRepoReader/Writer changes
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement preflight sizing computation
  - [x] 7.1 Add `_compute_stressor_sizing()` method to `PreflightChecker` in `preflight.py`
    - Computes cpu_request = allocatable_cpu // pod_ceiling, memory_request = allocatable_mem // pod_ceiling
    - Returns `StressorSizing` dataclass
    - _Requirements: 2.1_
  - [x] 7.2 Update `run()` in `preflight.py` to call `_compute_stressor_sizing()` after computing PodSizingRecommendation, use computed values for capacity demand calculations, include `stressor_sizing` in PreflightReport, fall back to None if NodeClass/NodePool data unavailable
    - _Requirements: 2.2, 2.3, 2.9, 2.10_
  - [ ]* 7.3 Write property test for dynamic sizing computation
    - **Property 1: Dynamic sizing computation produces correct requests and limits**
    - **Validates: Requirements 2.1, 2.7, 2.8**
  - [ ]* 7.4 Write property test for preflight capacity demand using dynamic sizing
    - **Property 2: Preflight uses dynamic sizing in capacity demand calculation**
    - **Validates: Requirements 2.2**
  - [ ]* 7.5 Write property test for preflight report completeness
    - **Property 3: Preflight report includes complete stressor sizing**
    - **Validates: Requirements 2.3**
  - [ ]* 7.6 Write unit test for preflight fallback when NodeClass data is missing
    - Test that `stressor_sizing` is None when `_get_nodeclass_configs()` returns empty
    - _Requirements: 2.10_

- [x] 8. Checkpoint — Verify preflight changes
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Implement controller orchestration changes
  - [x] 9.1 Update `run()` in `controller.py` to read `stressor_sizing` from PreflightReport and patch manifests via `set_resource_requests()` for stressor-role deployments
    - _Requirements: 2.4_
  - [x] 9.2 Update `run()` in `controller.py` to filter deployments by `scale-test/role` label: separate stressor vs infrastructure vs unlabeled, log warnings for unlabeled
    - _Requirements: 10.2, 10.3, 10.4_
  - [x] 9.3 Update `run()` in `controller.py` to scale iperf3 servers first (fixed ratio from `iperf3_server_ratio`), wait for Ready, then scale stressors
    - _Requirements: 6.1, 6.2, 6.3_
  - [x] 9.4 Update `run()` in `controller.py` to use `distribute_pods_weighted()` when `stressor_weights` is configured, fall back to `distribute_pods_across_deployments()` otherwise
    - _Requirements: 4.2, 4.3, 9.2, 9.3_
  - [ ]* 9.5 Write property test for role-based filtering
    - **Property 8: Role-based filtering excludes non-stressor deployments**
    - **Validates: Requirements 6.2, 10.2, 10.4**

- [x] 10. Implement CLI changes
  - [x] 10.1 Add `--stressor-weights` argument to `cli.py`, parse JSON string into `TestConfig.stressor_weights`
    - _Requirements: 9.4_
  - [x] 10.2 Add `--cpu-limit-multiplier`, `--memory-limit-multiplier`, `--iperf3-server-ratio` arguments to `cli.py`
    - _Requirements: 2.7, 6.2_
  - [ ]* 10.3 Write unit tests for CLI argument parsing
    - Test `--stressor-weights` parsing and omission
    - _Requirements: 9.4_

- [x] 11. Write manifest content unit tests
  - [ ]* 11.1 Write unit tests for static manifest content verification
    - Test overlay-prod reference, kustomization resources, namespace name, conservative defaults, no sidecars, sysctl non-privileged, continuous loops, headless service, image-puller images/nodeSelector/resources
    - **Property 11: Stressor manifest compliance** (example-based verification)
    - _Requirements: 1.1, 1.2, 1.4, 2.6, 3.1, 3.2, 5.1, 5.2, 5.3, 5.4, 6.4, 7.1, 7.2, 7.3, 8.1, 8.2, 8.3, 8.4_

- [x] 12. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Property tests use `hypothesis` with `@settings(max_examples=100)`
- Checkpoints ensure incremental validation after each major component
- The implementation order ensures no orphaned code: models first, then manifests, then reader/writer, then preflight, then controller, then CLI
