# Requirements Document

## Introduction

This feature integrates ClusterLoader2 (CL2) as an optional preload phase in the existing Kubernetes pod scaling test. When enabled via `--cl2-preload <config-name>`, CL2 populates the cluster with realistic background objects (services, configmaps, secrets, daemonsets, small deployments) before the pod scaling test begins. This simulates a real cluster's "furniture" so the scaling test runs against a non-empty cluster. The two-phase approach is:

1. **Phase 1 (CL2 Preload)**: CL2 creates background objects on the `alt` NodePool via a Kubernetes Job triggered through Flux GitOps.
2. **Phase 2 (Pod Scaling)**: The existing pod scaling test runs on top of that background load, scaling to 30,000+ pods.

CL2 preload results (PodStartupLatency, APIResponsiveness, SchedulingThroughput) are captured in the same test run's evidence store and rendered alongside pod scaling metrics in a single HTML report. Running without `--cl2-preload` behaves exactly as today.

## Glossary

- **CL2**: ClusterLoader2, the Kubernetes SIG-scalability load testing tool (`gcr.io/k8s-staging-perf-tests/clusterloader2`)
- **CL2_Config**: A YAML file in CL2's own format that defines test scenarios, namespaces, tuning sets, and measurement collectors
- **CL2_Job**: A Kubernetes Job resource that runs the CL2 binary with a mounted CL2_Config
- **CL2_Preload**: The phase where CL2 creates background objects in the cluster before pod scaling begins
- **Scale_Test_Controller**: The existing `ScaleTestController` class in `src/k8s_scale_test/controller.py` that orchestrates the full test lifecycle
- **Evidence_Store**: The `EvidenceStore` class that persists test artifacts (rate_data.jsonl, events.jsonl, findings/, summary.json) to disk
- **Scale_Test_CLI**: The `k8s-scale-test` command-line tool defined in `src/k8s_scale_test/cli.py`
- **Flux_Repo**: The GitOps repository at `flux2/` containing Kubernetes manifests managed by Flux
- **CL2_Result_Parser**: A module that reads CL2 JSON output and converts it into Evidence_Store-compatible data structures
- **Alt_NodePool**: The Karpenter NodePool named `alt` with label `karpenter: alt` used for scale test workloads
- **SLI_Metric**: A Service Level Indicator metric collected by CL2 (e.g., PodStartupLatency, APIResponsivenessPrometheusSimple, SchedulingThroughput)
- **Chart_Generator**: The `chart.py` module that produces self-contained HTML charts from test run data
- **TestConfig**: The configuration dataclass in `models.py` that holds all test run parameters

## Requirements

### Requirement 1: CL2 Flux Manifests

**User Story:** As a scale test operator, I want CL2 deployed as a Flux-managed app in the GitOps repo, so that CL2 preload runs are triggered through the standard Flux reconciliation workflow.

#### Acceptance Criteria

1. THE Flux_Repo SHALL contain a `apps/base/clusterloader2/` directory with a `kustomization.yaml`, `namespace.yaml`, `serviceaccount.yaml`, `rbac.yaml`, `job.yaml`, and `configmap.yaml`
2. WHEN Flux reconciles the clusterloader2 kustomization, THE CL2_Job SHALL run in the `scale-test` namespace using the `clusterloader2` ServiceAccount
3. THE CL2_Job SHALL mount the CL2_Config as a ConfigMap volume at a well-known path
4. THE CL2_Job SHALL write results to an emptyDir volume mounted at `/results`
5. THE `rbac.yaml` SHALL define a ClusterRole with permissions to create, get, list, watch, update, patch, delete, and deletecollection on deployments, services, configmaps, secrets, namespaces, pods, replicasets, daemonsets, and jobs
6. THE `rbac.yaml` SHALL bind the ClusterRole to the `clusterloader2` ServiceAccount via a ClusterRoleBinding
7. WHEN the CL2_Job creates test workload objects, THE CL2_Config SHALL include a nodeSelector `karpenter: alt` so test pods schedule on the Alt_NodePool

### Requirement 2: CL2 Test Config Templates

**User Story:** As a scale test operator, I want parameterizable CL2 test config templates for different preload scenarios, so that I can populate the cluster with varying types and counts of background objects.

#### Acceptance Criteria

1. THE Flux_Repo SHALL contain CL2_Config templates in `apps/base/clusterloader2/configs/` for at least three scenarios: `mixed-workload`, `service-mesh-stress`, and `configmap-secret-churn`
2. WHEN the `mixed-workload` config is used, THE CL2_Config SHALL create parameterized counts of Deployments with replicas, ClusterIP Services, ConfigMaps of varying sizes (1KB, 10KB, 100KB), Secrets of varying sizes, and DaemonSets
3. WHEN the `service-mesh-stress` config is used, THE CL2_Config SHALL create parameterized counts of Services with endpoints and measure DNS resolution latency and endpoint propagation time
4. WHEN the `configmap-secret-churn` config is used, THE CL2_Config SHALL create and update ConfigMaps and Secrets at a configurable rate and measure API server latency under write load
5. THE CL2_Config templates SHALL use CL2 template variables with default values so that object counts are parameterizable from 1K to 100K+ objects
6. THE CL2_Config templates SHALL include CL2 measurement collectors for PodStartupLatency, APIResponsivenessPrometheusSimple, SchedulingThroughput, WaitForControlledPodsRunning, and ResourceUsageSummary

### Requirement 3: CLI Integration

**User Story:** As a scale test operator, I want an optional `--cl2-preload` flag on the existing CLI, so that I can enable the CL2 preload phase before pod scaling without changing the default workflow.

#### Acceptance Criteria

1. WHEN the `--cl2-preload` flag is not provided, THE Scale_Test_CLI SHALL execute the existing pod scaling test with identical behavior to the current implementation
2. WHEN `--cl2-preload <config-name>` is provided, THE Scale_Test_CLI SHALL pass the config name to the Scale_Test_Controller for CL2 preload execution before pod scaling
3. WHEN `--cl2-timeout <seconds>` is provided, THE Scale_Test_CLI SHALL use the specified value as the CL2_Job timeout (default: 3600 seconds)
4. WHEN `--cl2-params <KEY=VAL,...>` is provided, THE Scale_Test_CLI SHALL parse the comma-separated key-value pairs and pass them as CL2 template parameter overrides
5. THE TestConfig SHALL include optional fields `cl2_preload`, `cl2_timeout`, and `cl2_params` to carry CL2 configuration through the system

### Requirement 4: CL2 Preload Phase in Controller

**User Story:** As a scale test operator, I want the CL2 preload phase to run automatically between operator approval and pod scaling, so that the cluster is populated with background objects before the scaling test begins.

#### Acceptance Criteria

1. WHEN `cl2_preload` is set in TestConfig, THE Scale_Test_Controller SHALL execute a `_run_cl2_preload()` method after operator approval and before pod scaling begins
2. WHEN `_run_cl2_preload()` executes, THE Scale_Test_Controller SHALL read the CL2_Config template from `configs/`, inject it into the Flux_Repo ConfigMap, update the Job name with a timestamp suffix, and git commit and push to trigger Flux reconciliation
3. WHILE the CL2_Job is running, THE Scale_Test_Controller SHALL poll the Job status every 10 seconds and log progress
4. WHEN the CL2_Job completes successfully, THE Scale_Test_Controller SHALL retrieve the CL2 results from the Job pod logs and parse them into a CL2Summary
5. WHEN the CL2_Job completes, THE Scale_Test_Controller SHALL save the CL2Summary to the Evidence_Store before proceeding to pod scaling
6. WHEN the CL2 preload phase completes, THE Scale_Test_Controller SHALL leave the CL2 preload objects in the cluster so that pod scaling runs on top of the background load
7. WHEN the pod scaling test completes and cleanup finishes, THE Scale_Test_Controller SHALL trigger CL2 cleanup to remove the preload objects
8. IF the CL2_Job fails or times out, THEN THE Scale_Test_Controller SHALL log the failure, save partial results to the Evidence_Store, and prompt the operator to continue or abort the pod scaling phase

### Requirement 5: CL2 Result Parsing

**User Story:** As a scale test operator, I want CL2 JSON results parsed into the existing evidence store format, so that CL2 preload metrics are available alongside pod scaling data in the same test run.

#### Acceptance Criteria

1. THE CL2_Result_Parser SHALL parse CL2 JSON summary output into a structured CL2Summary data model
2. WHEN CL2 results contain PodStartupLatency data, THE CL2_Result_Parser SHALL extract percentile values (P50, P90, P99) and convert them into LatencyPercentile data points
3. WHEN CL2 results contain APIResponsivenessPrometheusSimple data, THE CL2_Result_Parser SHALL extract latency percentiles grouped by API verb and resource
4. WHEN CL2 results contain SchedulingThroughput data, THE CL2_Result_Parser SHALL extract the scheduling rate as pods per second
5. THE Evidence_Store SHALL provide `save_cl2_summary()` and `load_cl2_summary()` methods to persist and retrieve `cl2_summary.json` in the run directory
6. FOR ALL valid CL2Summary objects, serializing via `to_dict()` then deserializing via `from_dict()` SHALL produce an equivalent data structure (round-trip property)
7. IF the CL2 JSON output is malformed or missing expected fields, THEN THE CL2_Result_Parser SHALL raise a CL2ParseError with a descriptive error message without crashing

### Requirement 6: Chart Integration

**User Story:** As a scale test operator, I want the HTML chart to display CL2 preload SLI metrics alongside pod scaling data, so that I can visualize the full test run in a single report.

#### Acceptance Criteria

1. WHEN a test run includes CL2 preload data and `cl2_summary.json` exists, THE Chart_Generator SHALL render additional chart sections showing CL2 SLI metrics
2. WHEN rendering CL2 metrics, THE Chart_Generator SHALL display PodStartupLatency percentiles as a bar chart
3. WHEN rendering CL2 metrics, THE Chart_Generator SHALL display APIResponsiveness latency percentiles grouped by verb and resource
4. WHEN no `cl2_summary.json` exists in the run directory, THE Chart_Generator SHALL render the chart without CL2 sections (existing behavior preserved)
5. THE Chart_Generator SHALL remain a self-contained HTML file with inline Chart.js

### Requirement 7: Backward Compatibility

**User Story:** As a scale test operator, I want the existing pod scaling test to continue working unchanged when `--cl2-preload` is not specified, so that CL2 integration does not break the current workflow.

#### Acceptance Criteria

1. WHEN `--cl2-preload` is not specified, THE Scale_Test_CLI SHALL execute the existing pod scaling test with identical behavior to the current implementation
2. THE Evidence_Store interface SHALL remain backward compatible so that existing test run data can still be loaded and queried
3. THE existing data models in `models.py` SHALL retain their current serialization format so that previously saved `summary.json` and `rate_data.jsonl` files remain readable
4. WHEN `--cl2-preload` is used alongside `--include-apps`, THE Scale_Test_CLI SHALL run the CL2 preload phase first, then scale only the specified apps
