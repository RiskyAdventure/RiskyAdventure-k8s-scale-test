# Requirements Document

## Introduction

This feature replaces the current pause/grow application with a suite of stress-test deployments as the primary scaling workload for the K8s scale testing tool. The stress-test deployments exercise CPU, memory, network, and I/O subsystems on EKS nodes in a realistic fashion. Resource requests are dynamically computed by the preflight checker using live cluster data (allocatable CPU/memory, effective maxPods, daemonset count) so that each stressor type packs to the node's pod ceiling (~110 usable pods per node after daemonsets). This maintains the same pod density as the original pause app while adding real workload stress. The preflight checker is extended to include dynamic sizing in its report, and the controller reads the sizing from the report to patch manifests before committing to Git. The scale test controller is also updated to support weighted pod distribution across heterogeneous stress-test deployments.

## Glossary

- **Controller**: The Python scale test controller (`src/k8s_scale_test/controller.py`) that orchestrates scaling by discovering deployments, setting replica counts, committing to the Flux GitOps repo, and monitoring pod readiness.
- **PreflightChecker**: Component (`src/k8s_scale_test/preflight.py`) that validates cluster capacity before test execution. Currently computes allocatable CPU/memory, effective maxPods, and PodSizingRecommendation per instance type. Will be extended to compute dynamic per-pod resource sizing and use those values in its capacity checks.
- **PreflightReport**: The output of the PreflightChecker. Currently includes EC2 quotas, subnet IPs, nodepool capacities, pod sizing recommendations, and a go/no-go decision. Will be extended to include computed `StressorSizing` data.
- **FluxRepoReader**: Component that scans the Flux GitOps repo (`flux2/apps/`) to discover Deployment resources. Reads resource requests from manifest YAML files.
- **FluxRepoWriter**: Component that updates replica counts and resource requests in Flux GitOps repo manifests and commits changes.
- **Stressor_Deployment**: A Kubernetes Deployment running stress-ng or iperf3 workloads that exercises a specific node subsystem (CPU, memory, network, or I/O).
- **Distribution_Strategy**: The algorithm the Controller uses to allocate a target pod count across multiple Stressor_Deployments with different resource profiles.
- **Dynamic_Sizing**: The process of computing per-pod resource requests from live cluster data so that each stressor type fills the node's pod ceiling. Computed inside the PreflightChecker, included in the PreflightReport, and applied by the Controller before committing to Git.
- **Pod_Ceiling**: The effective maxPods per node minus daemonset slots (~150 maxPods - ~40 daemonsets = ~110 usable). All stressor types must size their requests to fit this many pods per node.
- **Weight**: A relative proportion (expressed as a fraction of 1.0) that determines what share of the total target pods a given Stressor_Deployment receives.
- **Overlay_Prod**: The Kustomize overlay at `flux2/apps/overlay-prod/` that controls which apps are deployed to the production EKS cluster.
- **Image_Puller**: A DaemonSet that pre-caches container images on nodes to reduce pod startup latency during scale-up.
- **i4i_8xlarge**: The target node instance type with 32 vCPU, 256 GiB memory, and maxPods of 150.

## Requirements

### Requirement 1: Replace Pause App with Stress-Test App in Flux Manifests

**User Story:** As a scale test operator, I want the stress-test deployments to replace the pause/grow app as the primary scaling workload, so that scale tests exercise real node subsystems instead of idle pause containers.

#### Acceptance Criteria

1. WHEN the Overlay_Prod kustomization is applied, THE Overlay_Prod SHALL reference the stress-test base app instead of the pause base app.
2. THE stress-test kustomization SHALL include only Deployment-kind resources (no CronJobs), a namespace resource, and optionally the Image_Puller DaemonSet.
3. WHEN the FluxRepoReader scans the Flux repo, THE FluxRepoReader SHALL discover all Stressor_Deployments from the stress-test app directory.
4. THE stress-test namespace resource SHALL define a namespace named "stress-test" (replacing the pause app's "dns" namespace).

### Requirement 2: Dynamic Resource Sizing via Preflight

**User Story:** As a scale test operator, I want stress-test pod resource requests dynamically computed by the preflight checker using live cluster data, so that each stressor type packs to the node's pod ceiling (~110 pods per node) regardless of the underlying instance type, and the preflight capacity checks use the correct resource values.

#### Acceptance Criteria

1. THE PreflightChecker SHALL compute per-pod resource requests using the formula: `cpu_request = allocatable_cpu_millicores / pod_ceiling` and `memory_request = allocatable_memory_mi / pod_ceiling`, where `pod_ceiling` is `effective_max_pods - daemonset_count`. This uses data the PreflightChecker already queries (NodePool configs, NodeClass configs, daemonset count).
2. THE PreflightChecker SHALL use the dynamically computed resource requests (instead of the conservative defaults from the manifest files) when calculating total workload demand for the vCPU quota check, NodePool CPU limit check, and max_achievable_pods calculation.
3. THE PreflightReport SHALL include a new `stressor_sizing` field containing the computed per-pod CPU request (millicores), memory request (MiB), the pod_ceiling used, and the source data (instance type, allocatable CPU/memory, daemonset count) so the Controller and operator can inspect the sizing decision.
4. AFTER preflight completes, THE Controller SHALL read the `stressor_sizing` from the PreflightReport and use the FluxRepoWriter to patch the Stressor_Deployment manifests with the computed resource requests BEFORE committing the replica count changes to Git.
5. THE FluxRepoWriter SHALL support a `set_resource_requests(name, cpu_request, memory_request, source_path)` method that updates the resource requests in a Deployment manifest.
6. THE Stressor_Deployment manifests SHALL ship with conservative default resource requests (e.g., 10m CPU, 128Mi memory — matching the original pause app) that are overwritten by the Controller at runtime using the preflight-computed values.
7. THE Stressor_Deployments SHALL set resource limits as a configurable multiplier of the computed requests (default: 2x for CPU, 1.5x for memory) to allow controlled oversubscription while preventing runaway resource consumption.
8. THE computed resource requests SHALL target the Pod_Ceiling so that the total pod count across all nodes matches the target_pods configuration, maintaining the same ~30K node provisioning as the pause app.
9. THE PreflightChecker's existing capacity checks (subnet IPs, vCPU quota, NodePool CPU limits, pod ceiling) SHALL continue to function correctly with the dynamically computed values, producing the same go/no-go decision quality as before.
10. WHEN the PreflightChecker cannot determine the instance type or NodeClass configuration (e.g., missing CRDs), THE PreflightChecker SHALL fall back to the manifest's existing resource requests and log a warning, preserving current behavior.

### Requirement 3: Convert CronJob to Continuous Deployments

**User Story:** As a scale test operator, I want all stress workloads to run as Deployments with 0 initial replicas, so that the Controller can scale them like any other deployment.

#### Acceptance Criteria

1. THE stress-test app SHALL contain only Deployment-kind workloads (no CronJob resources) for stressor pods.
2. WHEN a Stressor_Deployment pod starts, THE stress-ng or iperf3 command SHALL run in a continuous loop (with configurable rest intervals) instead of exiting after a single run.
3. THE io-stress Stressor_Deployment SHALL replace the CronJob's one-shot stress-ng invocation with a continuously looping Deployment that exercises CPU, memory, and I/O mix workloads.
4. WHEN the Controller sets replicas to 0 during cleanup, THE Stressor_Deployment pods SHALL terminate gracefully within 30 seconds.

### Requirement 4: Weighted Pod Distribution in the Controller

**User Story:** As a scale test operator, I want the Controller to distribute pods across Stressor_Deployments according to configurable weights, so that different stressor types receive appropriate shares of the total pod count based on their resource profiles.

#### Acceptance Criteria

1. THE Distribution_Strategy SHALL accept a mapping of deployment names to Weights (floats summing to 1.0).
2. WHEN Weights are provided, THE Distribution_Strategy SHALL allocate pods proportionally: each Stressor_Deployment receives `floor(total_target * weight)` pods, with remainder pods distributed round-robin starting from the highest-weighted deployment.
3. WHEN no Weights are provided, THE Distribution_Strategy SHALL fall back to even distribution (current behavior).
4. IF the Weights do not sum to 1.0 (within a tolerance of 0.01), THEN THE Controller SHALL raise a configuration error before scaling begins.
5. THE DeploymentConfig model SHALL include an optional `weight` field that the Distribution_Strategy reads when allocating pods.
6. WHEN the Controller logs the distribution plan, THE Controller SHALL include each deployment name, its weight, and its allocated replica count.

### Requirement 5: Remove Validator Sidecars from Stressor Deployments

**User Story:** As a scale test operator, I want validator sidecars removed from stress-test deployments, so that stressor pods have lower overhead and do not require privileged host access that complicates security posture at 30K-node scale.

#### Acceptance Criteria

1. THE cpu-stress Stressor_Deployment SHALL contain only the stress-ng container (no validator sidecar, no hostPath volume mounts).
2. THE memory-stress Stressor_Deployment SHALL contain only the stress-ng container (no zram-validator sidecar, no privileged security context on sidecars).
3. THE network-stress server Stressor_Deployment SHALL contain only the iperf3 server container (no network-validator sidecar, no privileged security context on sidecars).
4. THE sysctl-stress Stressor_Deployment SHALL be converted to a non-privileged connection stressor that uses stress-ng `--sock` workers to stress the TCP stack locally, instead of curling external endpoints (which would generate millions of external requests at 30K-node scale).

### Requirement 6: Network Stress Architecture

**User Story:** As a scale test operator, I want iperf3 server pods deployed and ready before client pods scale up, so that network stress clients have endpoints to connect to.

#### Acceptance Criteria

1. WHEN the Controller begins a scale test, THE Controller SHALL deploy iperf3 server pods before scaling client Stressor_Deployments.
2. THE iperf3 server Stressor_Deployment SHALL be excluded from the weighted Distribution_Strategy and instead maintain a fixed replica count proportional to the number of client pods (e.g., 1 server per 50 clients).
3. WHEN iperf3 server pods reach Ready state, THE Controller SHALL proceed to scale the remaining Stressor_Deployments (including iperf3 clients).
4. THE iperf3 headless Service SHALL be included in the stress-test kustomization to enable DNS-based service discovery for clients.

### Requirement 7: Image Pre-caching DaemonSet

**User Story:** As a scale test operator, I want the image-puller DaemonSet updated to cache the correct stress-test container images, so that pod startup latency is minimized during large-scale scale-up.

#### Acceptance Criteria

1. THE Image_Puller DaemonSet SHALL pre-cache the stress-ng and iperf3 container images used by the Stressor_Deployments.
2. THE Image_Puller DaemonSet SHALL target nodes in the `karpenter.sh/nodepool: alt` nodepool using a nodeSelector.
3. THE Image_Puller DaemonSet SHALL use minimal resource requests (10m CPU, 10Mi memory) to avoid impacting node capacity calculations.
4. THE Image_Puller DaemonSet SHALL be included in the stress-test kustomization.

### Requirement 8: Stressor Deployment Scheduling Constraints

**User Story:** As a scale test operator, I want stress-test pods scheduled only on Karpenter-managed nodes with appropriate spread, so that stress is distributed evenly across the node fleet.

#### Acceptance Criteria

1. THE Stressor_Deployments SHALL use a nodeSelector of `karpenter.sh/nodepool: alt` to target Karpenter-managed nodes.
2. THE Stressor_Deployments SHALL include topology spread constraints with `topologyKey: topology.kubernetes.io/zone` and `maxSkew: 10` using `ScheduleAnyway` policy.
3. THE Stressor_Deployments SHALL include topology spread constraints with `topologyKey: kubernetes.io/hostname` and `maxSkew: 10` using `ScheduleAnyway` policy.
4. THE Stressor_Deployments SHALL use a rolling update strategy with `maxSurge: 5000` and `maxUnavailable: 0` to support rapid scale-up.

### Requirement 9: Controller Configuration for Stress-Test Weights

**User Story:** As a scale test operator, I want to configure stressor weights via the existing TestConfig, so that I can tune the distribution mix without modifying manifests.

#### Acceptance Criteria

1. THE TestConfig model SHALL include an optional `stressor_weights` field: a dictionary mapping deployment names to float weights.
2. WHEN `stressor_weights` is provided in the configuration, THE Controller SHALL pass the weights to the Distribution_Strategy.
3. WHEN `stressor_weights` is omitted, THE Controller SHALL use even distribution across all discovered deployments.
4. THE CLI SHALL accept a `--stressor-weights` argument as a JSON string (e.g., `'{"cpu-stress-test": 0.4, "memory-stress-test": 0.3, "io-stress-test": 0.2, "iperf3-client": 0.1}'`).

### Requirement 10: Deployment Discovery Filtering

**User Story:** As a scale test operator, I want the FluxRepoReader to distinguish between scalable stressor deployments and infrastructure deployments (like iperf3 servers), so that the Controller only distributes pods across the correct set of deployments.

#### Acceptance Criteria

1. WHEN the FluxRepoReader discovers deployments, THE FluxRepoReader SHALL read a `scale-test/role` label from each Deployment's metadata labels.
2. THE Distribution_Strategy SHALL only distribute pods across deployments labeled with `scale-test/role: stressor`.
3. THE Controller SHALL handle deployments labeled with `scale-test/role: infrastructure` separately (e.g., iperf3 servers are scaled with a fixed ratio, not via weighted distribution).
4. IF a discovered deployment has no `scale-test/role` label, THEN THE Controller SHALL log a warning and exclude the deployment from distribution.
