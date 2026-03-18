# Requirements Document

## Introduction

The preflight checker in the k8s-scale-test CLI performs observability connectivity checks (AMP, CloudWatch Logs, EKS API) before a scale test run. Currently these checks have zero unit test coverage, which allowed a bug (EKS API not using the Wesley beta endpoint URL) to go undetected. Additionally, when observability checks fail, the preflight silently continues with a warning — the operator is never prompted to decide whether to proceed or abort. This feature adds comprehensive unit tests for all three observability checks and changes the failure behavior to prompt the operator interactively.

## Glossary

- **Preflight_Checker**: The `PreflightChecker` class in `src/k8s_scale_test/preflight.py` that validates cluster readiness before a scale test
- **Observability_Check**: One of three connectivity checks (AMP, CloudWatch Logs, EKS API) performed during preflight step 5
- **AMP_Check**: The observability check that verifies connectivity to Amazon Managed Prometheus by querying the `up` metric via `AMPMetricCollector`
- **CloudWatch_Check**: The observability check that verifies the configured CloudWatch log group exists via `describe_log_groups`
- **EKS_API_Check**: The observability check that verifies the EKS cluster is ACTIVE via `describe_cluster`, optionally using a custom `aws_endpoint_url`
- **Operator_Prompt**: The interactive prompt mechanism (`_prompt_operator`) that presents information and waits for operator input
- **Auto_Approve_Mode**: A configuration flag (`auto_approve=True`) that bypasses interactive prompts for CI/automation pipelines
- **Controller**: The `ScaleTestController` class in `src/k8s_scale_test/controller.py` that orchestrates the scale test lifecycle
- **GoNoGo_Decision**: The preflight verdict (`GO` or `NO_GO`) based on capacity constraint checks
- **TestConfig**: The configuration dataclass containing all scale test parameters including `aws_endpoint_url`, `amp_workspace_id`, `cloudwatch_log_group`, and `eks_cluster_name`

## Requirements

### Requirement 1: AMP Connectivity Check Unit Tests

**User Story:** As a developer, I want unit tests for the AMP connectivity check, so that regressions in AMP connectivity validation are caught before deployment.

#### Acceptance Criteria

1. WHEN the AMP_Check executes with a valid `amp_workspace_id` and the AMP query returns status "success", THE Preflight_Checker SHALL count the AMP_Check as passed
2. WHEN the AMP_Check executes with a valid `amp_workspace_id` and the AMP query returns a non-success status, THE Preflight_Checker SHALL add a recommendation indicating the unexpected AMP status
3. WHEN the AMP_Check executes and the AMP query raises an exception, THE Preflight_Checker SHALL add a recommendation indicating AMP connectivity failure and log a warning
4. WHEN the AMP_Check executes with no `amp_workspace_id` and no `prometheus_url` configured, THE Preflight_Checker SHALL skip the check and add a recommendation that AMP is not configured

### Requirement 2: CloudWatch Logs Check Unit Tests

**User Story:** As a developer, I want unit tests for the CloudWatch Logs connectivity check, so that regressions in log group validation are caught before deployment.

#### Acceptance Criteria

1. WHEN the CloudWatch_Check executes with a valid `cloudwatch_log_group` and the log group exists, THE Preflight_Checker SHALL count the CloudWatch_Check as passed
2. WHEN the CloudWatch_Check executes with a valid `cloudwatch_log_group` and the log group does not exist in the response, THE Preflight_Checker SHALL add a recommendation indicating the log group was not found
3. WHEN the CloudWatch_Check executes and the `describe_log_groups` call raises an exception, THE Preflight_Checker SHALL add a recommendation indicating CloudWatch Logs connectivity failure
4. WHEN the CloudWatch_Check executes with no `cloudwatch_log_group` configured, THE Preflight_Checker SHALL skip the check and add a recommendation that CloudWatch is not configured

### Requirement 3: EKS API Check Unit Tests

**User Story:** As a developer, I want unit tests for the EKS API connectivity check, so that endpoint URL configuration bugs are caught before deployment.

#### Acceptance Criteria

1. WHEN the EKS_API_Check executes with a valid `eks_cluster_name` and `aws_endpoint_url` is configured, THE Preflight_Checker SHALL pass `endpoint_url` to the boto3 EKS client constructor
2. WHEN the EKS_API_Check executes with a valid `eks_cluster_name` and no `aws_endpoint_url` is configured, THE Preflight_Checker SHALL create the boto3 EKS client without an `endpoint_url` parameter
3. WHEN the EKS_API_Check executes and the cluster status is "ACTIVE", THE Preflight_Checker SHALL count the EKS_API_Check as passed
4. WHEN the EKS_API_Check executes and the cluster status is not "ACTIVE", THE Preflight_Checker SHALL add a recommendation indicating the non-ACTIVE cluster status
5. WHEN the EKS_API_Check executes and the `describe_cluster` call raises an exception, THE Preflight_Checker SHALL add a recommendation indicating EKS API connectivity failure
6. WHEN the EKS_API_Check executes with no `eks_cluster_name` configured, THE Preflight_Checker SHALL skip the check and add a recommendation that EKS cluster name is not configured

### Requirement 4: Operator Prompt on Observability Failure

**User Story:** As a scale test operator, I want to be prompted when observability checks fail, so that I can make an informed decision about whether to proceed without full investigation capability.

#### Acceptance Criteria

1. WHEN one or more Observability_Checks fail during preflight, THE Controller SHALL prompt the operator with a summary of the failed checks and ask whether to continue or abort
2. WHEN the operator responds with "abort" or "cancel" to the observability failure prompt, THE Controller SHALL halt the scale test run
3. WHEN the operator responds with "continue" to the observability failure prompt, THE Controller SHALL proceed with the scale test run
4. WHILE Auto_Approve_Mode is enabled and one or more Observability_Checks fail, THE Controller SHALL log the failures clearly and continue without prompting
5. WHEN all Observability_Checks pass, THE Controller SHALL proceed without prompting the operator about observability
6. WHEN Observability_Checks fail, THE GoNoGo_Decision SHALL remain based on capacity constraint checks only and not be affected by observability failures

### Requirement 5: Observability Check Result Reporting

**User Story:** As a scale test operator, I want observability check results included in the preflight report, so that I can review which checks passed or failed.

#### Acceptance Criteria

1. THE Preflight_Checker SHALL return the count of passed and total observability checks in the PreflightReport
2. WHEN Observability_Checks fail, THE Preflight_Checker SHALL include the specific failure reasons in the recommendations list of the GoNoGo_Decision
