# Implementation Plan: Preflight Observability Checks

## Overview

Add unit test coverage for all three observability connectivity checks (AMP, CloudWatch Logs, EKS API) in `PreflightChecker`, expose observability results on `PreflightReport`, and add an operator prompt in the controller when observability checks fail.

## Tasks

- [x] 1. Add observability result fields to PreflightReport
  - [x] 1.1 Add `obs_passed: int = 0` and `obs_total: int = 0` fields to `PreflightReport` in `src/k8s_scale_test/models.py`
    - _Requirements: 5.1_
  - [x] 1.2 Update `PreflightChecker.run()` in `src/k8s_scale_test/preflight.py` to set `obs_passed` and `obs_total` on the returned `PreflightReport` using the existing `obs_ok` and `obs_total` local variables
    - _Requirements: 5.1, 5.2_

- [x] 2. Write unit tests for AMP connectivity check
  - [x] 2.1 Create `tests/test_preflight_observability.py` with shared test fixtures: a helper to build a `PreflightChecker` with mocked `aws_client`/`k8s_client` and mocked internal methods (`_get_ec2_quotas`, `_get_subnet_ip_availability`, `_get_nodepool_configs`, `_get_nodeclass_configs`, `_count_daemonsets`) returning minimal valid data
    - _Requirements: 1.1, 1.2, 1.3, 1.4_
  - [x] 2.2 Write test: AMP check passes when `amp_workspace_id` is set and `_query_promql` returns `{"status": "success"}` — verify `obs_passed` increments and no AMP recommendation added
    - _Requirements: 1.1_
  - [x] 2.3 Write test: AMP check adds recommendation when `_query_promql` returns non-success status
    - _Requirements: 1.2_
  - [x] 2.4 Write test: AMP check adds recommendation when `_query_promql` raises an exception
    - _Requirements: 1.3_
  - [x] 2.5 Write test: AMP check skipped when neither `amp_workspace_id` nor `prometheus_url` is configured
    - _Requirements: 1.4_

- [x] 3. Write unit tests for CloudWatch Logs connectivity check
  - [x] 3.1 Write test: CloudWatch check passes when `cloudwatch_log_group` is set and `describe_log_groups` returns the matching group
    - _Requirements: 2.1_
  - [x] 3.2 Write test: CloudWatch check adds recommendation when log group not found in response
    - _Requirements: 2.2_
  - [x] 3.3 Write test: CloudWatch check adds recommendation when `describe_log_groups` raises an exception
    - _Requirements: 2.3_
  - [x] 3.4 Write test: CloudWatch check skipped when `cloudwatch_log_group` is not configured
    - _Requirements: 2.4_

- [x] 4. Write unit tests for EKS API connectivity check
  - [x] 4.1 Write test: EKS check passes `endpoint_url` to boto3 client when `aws_endpoint_url` is configured — verify mock `aws_client.client("eks", ...)` called with `endpoint_url` kwarg
    - _Requirements: 3.1_
  - [x] 4.2 Write test: EKS check does NOT pass `endpoint_url` when `aws_endpoint_url` is empty/None
    - _Requirements: 3.2_
  - [x] 4.3 Write test: EKS check passes when cluster status is "ACTIVE"
    - _Requirements: 3.3_
  - [x] 4.4 Write test: EKS check adds recommendation when cluster status is not "ACTIVE"
    - _Requirements: 3.4_
  - [x] 4.5 Write test: EKS check adds recommendation when `describe_cluster` raises an exception
    - _Requirements: 3.5_
  - [x] 4.6 Write test: EKS check skipped when `eks_cluster_name` is not configured
    - _Requirements: 3.6_

- [x] 5. Checkpoint - Ensure all preflight observability tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Add operator prompt for observability failures in controller
  - [x] 6.1 In `ScaleTestController.run()` in `src/k8s_scale_test/controller.py`, after the preflight report is saved and before Phase 2 operator approval, add logic to check `report.obs_total > 0 and report.obs_passed < report.obs_total` and call `_prompt_operator` with the failure summary. If operator responds "abort"/"cancel", return early with `halt_reason="operator_aborted_observability"`.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.6_

- [x] 7. Write unit tests for controller observability prompt
  - [x] 7.1 Create `tests/test_controller_observability_prompt.py` with test: `_prompt_operator` is called when `obs_passed < obs_total`
    - _Requirements: 4.1_
  - [x] 7.2 Write test: operator "abort" response halts the run with `halt_reason="operator_aborted_observability"`
    - _Requirements: 4.2_
  - [x] 7.3 Write test: operator "continue" response proceeds past observability check
    - _Requirements: 4.3_
  - [x] 7.4 Write test: `auto_approve=True` continues without interactive prompt when obs checks fail
    - _Requirements: 4.4_
  - [x] 7.5 Write test: no observability prompt when `obs_passed == obs_total`
    - _Requirements: 4.5_

- [ ]* 8. Write property-based tests for correctness properties
  - [ ]* 8.1 Write property test: for any PreflightReport with obs_passed < obs_total, the controller calls _prompt_operator with observability context
    - **Property 1: Controller prompts operator on observability failure**
    - **Validates: Requirements 4.1**
  - [ ]* 8.2 Write property test: for any combination of observability config, 0 <= obs_passed <= obs_total AND decision depends only on blocking_constraints
    - **Property 2: Preflight report observability invariants**
    - **Validates: Requirements 4.6, 5.1**

- [x] 9. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- The existing `_prompt_operator` / `auto_approve` mechanism handles CI mode automatically — no new prompt infrastructure needed
- Property tests use `hypothesis` (already available in the project's test dependencies)
- All mocking uses `unittest.mock` from the standard library
