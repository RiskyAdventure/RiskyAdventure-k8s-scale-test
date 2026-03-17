"""Data models for the Kubernetes scale testing system."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, Any, Type, TypeVar, List, Dict

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Helpers for JSON round-trip
# ---------------------------------------------------------------------------

def _serialize(value: Any) -> Any:
    """Recursively serialize a value for JSON output."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    return value


def _deserialize(value: Any, target_type: Any) -> Any:
    """Recursively deserialize a value into *target_type*."""
    if value is None:
        return None

    origin = getattr(target_type, "__origin__", None)

    # Optional[X] → unwrap
    if origin is type(None):
        return None
    # Handle Optional (Union[X, None])
    args = getattr(target_type, "__args__", None)
    if args and type(None) in args:
        inner = [a for a in args if a is not type(None)][0]
        return _deserialize(value, inner)

    # list[X]
    if origin is list:
        item_type = args[0] if args else Any
        return [_deserialize(v, item_type) for v in value]

    # dict[K, V]
    if origin is dict:
        k_type = args[0] if args else Any
        v_type = args[1] if args else Any
        return {_deserialize(k, k_type): _deserialize(v, v_type) for k, v in value.items()}

    # datetime
    if target_type is datetime:
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(value)

    # Enum subclass
    if isinstance(target_type, type) and issubclass(target_type, Enum):
        return target_type(value)

    # Dataclass with from_dict
    if isinstance(target_type, type) and hasattr(target_type, "from_dict"):
        if isinstance(value, dict):
            return target_type.from_dict(value)
        return value

    return value


def _get_type_hints(cls: type) -> Dict[str, Any]:
    """Return resolved type hints for *cls* using get_type_hints."""
    import typing
    return typing.get_type_hints(cls)


class _SerializableMixin:
    """Mixin that adds ``to_dict`` / ``from_dict`` to dataclasses."""

    def to_dict(self) -> dict:
        result: dict = {}
        for f in fields(self):  # type: ignore[arg-type]
            result[f.name] = _serialize(getattr(self, f.name))
        return result

    @classmethod
    def from_dict(cls: Type[T], data: dict) -> T:
        hints = _get_type_hints(cls)
        kwargs: dict = {}
        for f in fields(cls):  # type: ignore[arg-type]
            if f.name not in data:
                continue
            kwargs[f.name] = _deserialize(data[f.name], hints[f.name])
        return cls(**kwargs)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class GoNoGo(Enum):
    GO = "go"
    NO_GO = "no_go"


class AlertType(Enum):
    RATE_DROP = "rate_drop"
    PENDING_TIMEOUT = "pending_timeout"
    NODE_NOT_READY = "node_not_ready"
    MONITOR_GAP = "monitor_gap"


class Severity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class RunValidity(Enum):
    VALID = "valid"
    INVALID = "invalid"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class TestConfig(_SerializableMixin):
    target_pods: int
    batch_size: int = 50
    batch_interval_seconds: float = 10.0
    rate_drop_threshold_pct: float = 75.0
    pending_timeout_seconds: float = 120.0
    rolling_avg_window_seconds: float = 30.0
    kubeconfig: Optional[str] = None
    aws_profile: Optional[str] = None
    aws_endpoint_url: str = "https://api.beta.us-west-2.wesley.amazonaws.com"
    flux_repo_path: str = "/Users/shancor/Projects/flux/flux2"
    output_dir: str = "./scale-test-results"
    prometheus_url: Optional[str] = None
    event_time_window_minutes: int = 5
    ssm_log_lines: int = 500
    ssm_journal_minutes: int = 10
    auto_approve: bool = False
    exclude_apps: list = None
    include_apps: list = None
    cl2_preload: Optional[str] = None
    cl2_timeout: float = 3600.0
    cl2_params: Optional[Dict[str, str]] = None
    hold_at_peak: int = 90
    stressor_weights: Optional[Dict[str, float]] = None
    cpu_limit_multiplier: float = 2.0
    memory_limit_multiplier: float = 1.5
    iperf3_server_ratio: int = 50
    amp_workspace_id: Optional[str] = None
    cloudwatch_log_group: Optional[str] = None
    eks_cluster_name: Optional[str] = None
    kb_table_name: str = "scale-test-kb"
    kb_s3_bucket: Optional[str] = None
    kb_s3_prefix: str = "kb-entries/"
    kb_match_threshold: float = 0.7
    kb_auto_ingest: bool = True


# ---------------------------------------------------------------------------
# Preflight Models
# ---------------------------------------------------------------------------

@dataclass
class PodsPerNodeBreakdown(_SerializableMixin):
    instance_type: str
    nodepool_name: str
    total_cores: int
    max_pods_setting: int
    pods_per_core_setting: int
    pods_per_core_limit: int          # total_cores * pods_per_core_setting
    effective_max_pods: int            # min(max_pods_setting, pods_per_core_limit)
    system_reserved_cpu: str
    kube_reserved_cpu: str
    system_reserved_memory: str
    kube_reserved_memory: str
    eviction_hard_memory: str


@dataclass
class PodSizingRecommendation(_SerializableMixin):
    instance_type: str
    allocatable_cpu_millicores: int
    allocatable_memory_mi: int
    recommended_cpu_request: str
    recommended_memory_request: str
    max_pods_by_cpu: int
    max_pods_by_memory: int
    effective_density_limit: int


@dataclass
class StressorSizing(_SerializableMixin):
    cpu_request_millicores: int
    memory_request_mi: int
    pod_ceiling: int
    instance_type: str
    allocatable_cpu_millicores: int
    allocatable_memory_mi: int
    daemonset_count: int
    cpu_limit_multiplier: float = 2.0
    memory_limit_multiplier: float = 1.5


@dataclass
class SubnetIPInfo(_SerializableMixin):
    subnet_id: str
    availability_zone: str
    available_ips: int
    cidr_block: str


@dataclass
class EC2Quotas(_SerializableMixin):
    current_vcpu_usage: int
    vcpu_quota: int
    headroom_vcpus: int
    usage_pct: float


@dataclass
class NodePoolConfig(_SerializableMixin):
    name: str
    cpu_limit: int
    memory_limit_gi: int
    instance_types: List[str]
    architecture: str
    capacity_type: str
    node_class_name: str = ""


@dataclass
class NodeClassConfig(_SerializableMixin):
    name: str
    max_pods: int
    pods_per_core: int
    system_reserved_cpu: str
    system_reserved_memory: str
    kube_reserved_cpu: str
    kube_reserved_memory: str
    eviction_hard_memory: str
    subnet_discovery_tag: str


@dataclass
class NodePoolCapacity(_SerializableMixin):
    name: str
    instance_types: List[str]
    vcpus_per_instance: Dict[str, int]
    max_nodes_per_type: Dict[str, int]
    max_pods_per_type: Dict[str, int]
    total_max_pods: int


@dataclass
class GoNoGoDecision(_SerializableMixin):
    decision: GoNoGo
    constraints_checked: List[str]
    blocking_constraints: List[str]
    recommendations: List[str]


@dataclass
class PreflightReport(_SerializableMixin):
    timestamp: datetime
    config: TestConfig
    ec2_quotas: EC2Quotas
    subnet_ips: List[SubnetIPInfo]
    total_available_ips: int
    nodepool_capacities: List[NodePoolCapacity]
    pods_per_node_breakdowns: List[PodsPerNodeBreakdown]
    pod_sizing_recommendations: List[PodSizingRecommendation]
    max_achievable_pods: int
    decision: GoNoGoDecision
    stressor_sizing: Optional[StressorSizing] = None


# ---------------------------------------------------------------------------
# Monitoring Models
# ---------------------------------------------------------------------------

@dataclass
class RateDataPoint(_SerializableMixin):
    timestamp: datetime
    ready_count: int
    delta_ready: int
    ready_rate: float          # pods/sec over the interval
    rolling_avg_rate: float
    pending_count: int
    total_pods: int
    interval_seconds: float = 0.0  # actual seconds since last data point
    is_gap: bool = False           # True if interval > 2× expected (data was stale)


@dataclass
class Alert(_SerializableMixin):
    alert_type: AlertType
    timestamp: datetime
    message: str
    context: Dict[str, Any]


# ---------------------------------------------------------------------------
# Anomaly & Diagnostics Models
# ---------------------------------------------------------------------------

@dataclass
class K8sEvent(_SerializableMixin):
    timestamp: datetime
    namespace: str
    involved_object_kind: str
    involved_object_name: str
    reason: str
    message: str
    event_type: str
    scaling_step: int
    count: int


@dataclass
class NodeCondition(_SerializableMixin):
    node_name: str
    condition_type: str
    status: str
    reason: str
    message: str


@dataclass
class NodeMetric(_SerializableMixin):
    node_name: str
    cpu_usage_pct: float
    memory_usage_pct: float
    pod_count: int
    pods_ready: int
    pods_not_ready: int


@dataclass
class ProblemNode(_SerializableMixin):
    node_name: str
    instance_id: str
    reasons: List[str]
    metrics: NodeMetric
    conditions: List[NodeCondition]


@dataclass
class SSMCommandResult(_SerializableMixin):
    instance_id: str
    command: str
    command_id: str
    status: str
    output: str
    error: str


@dataclass
class NodeDiagnostic(_SerializableMixin):
    node_name: str
    instance_id: str
    collection_timestamp: datetime
    kubelet_logs: SSMCommandResult
    containerd_logs: SSMCommandResult
    journal_kubelet: SSMCommandResult
    journal_containerd: SSMCommandResult
    resource_utilization: SSMCommandResult


@dataclass
class Finding(_SerializableMixin):
    finding_id: str
    timestamp: datetime
    severity: Severity
    symptom: str
    affected_resources: List[str]
    k8s_events: List[K8sEvent]
    node_metrics: List[NodeMetric]
    node_diagnostics: List[NodeDiagnostic]
    evidence_references: List[str]
    root_cause: Optional[str] = None
    resolved: bool = False


# ---------------------------------------------------------------------------
# Scaling Models
# ---------------------------------------------------------------------------

@dataclass
class ScalingStep(_SerializableMixin):
    step_number: int
    timestamp: datetime
    target_replicas: int
    actual_ready: int
    actual_pending: int
    duration_seconds: float


@dataclass
class ScalingResult(_SerializableMixin):
    steps: List[ScalingStep]
    total_pods_requested: int
    total_pods_ready: int
    total_nodes_provisioned: int
    peak_ready_rate: float
    completed: bool
    halt_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Test Run Models
# ---------------------------------------------------------------------------

@dataclass
class TestRunSummary(_SerializableMixin):
    run_id: str
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    config: TestConfig
    preflight: PreflightReport
    scaling_result: ScalingResult
    peak_pod_count: int
    peak_ready_rate: float
    total_nodes_provisioned: int
    anomaly_count: int
    findings: List[Finding]
    validity: RunValidity
    validity_reason: str
    node_health_sweep: Optional[Dict] = None
    karpenter_health: Optional[Dict] = None
    agent_findings: Optional[List[Dict]] = None
    scanner_findings: Optional[List[Dict]] = None


# ---------------------------------------------------------------------------
# CL2 (ClusterLoader2) Models
# ---------------------------------------------------------------------------

@dataclass
class LatencyPercentile(_SerializableMixin):
    """A single latency percentile measurement."""
    metric_name: str
    percentile: str
    latency_ms: float


@dataclass
class APILatencyResult(_SerializableMixin):
    """API server latency for a specific verb/resource combination."""
    verb: str
    resource: str
    scope: str
    p50_ms: float
    p90_ms: float
    p99_ms: float
    count: int


@dataclass
class SchedulingThroughputResult(_SerializableMixin):
    """Scheduling throughput measurement."""
    pods_per_second: float
    total_pods_scheduled: int
    duration_seconds: float


@dataclass
class CL2PreloadPlan(_SerializableMixin):
    """Computed CL2 preload object counts, proportional to target pod scale."""
    target_pods: int
    namespaces: int
    deployments_per_ns: int
    pod_replicas: int
    services_per_ns: int
    configmaps_per_ns: int
    secrets_per_ns: int
    # Computed totals (planned)
    total_deployments: int = 0
    total_pods: int = 0
    total_services: int = 0
    total_configmaps: int = 0
    total_secrets: int = 0
    total_objects: int = 0
    # Actual counts (verified after CL2 runs)
    actual_namespaces: int = 0
    actual_deployments: int = 0
    actual_pods: int = 0
    actual_services: int = 0
    actual_configmaps: int = 0
    actual_secrets: int = 0
    actual_total: int = 0

    def __post_init__(self):
        self.total_deployments = self.namespaces * self.deployments_per_ns
        self.total_pods = self.total_deployments * self.pod_replicas
        self.total_services = self.namespaces * self.services_per_ns
        self.total_configmaps = self.namespaces * self.configmaps_per_ns * 3  # 1KB + 10KB + 100KB
        self.total_secrets = self.namespaces * self.secrets_per_ns
        self.total_objects = (
            self.namespaces  # namespaces themselves
            + self.total_deployments
            + self.total_pods
            + self.total_services
            + self.total_configmaps
            + self.total_secrets
        )

    @staticmethod
    def from_target_pods(target_pods: int) -> "CL2PreloadPlan":
        """Compute a diverse preload plan proportional to the target pod count.

        Ratios:
        - Deployments: target_pods / 40 (40 pods per deployment)
        - Namespaces: deployments / 2
        - Services: same as deployments
        - ConfigMaps: 1 per target pod, spread across namespaces (× 3 sizes)
        - Secrets: 1 per target pod, spread across namespaces
        """
        total_deployments = max(5, target_pods // 40)
        namespaces = max(3, total_deployments // 2)
        deployments_per_ns = max(1, total_deployments // namespaces)
        pod_replicas = 0  # Deployments created with 0 replicas to avoid provisioning nodes
        services_per_ns = deployments_per_ns

        # 1:1 configmaps to target pods, spread across namespaces (× 3 sizes)
        configmaps_per_ns = max(1, target_pods // (namespaces * 3))
        # 1:1 secrets to target pods, spread across namespaces
        secrets_per_ns = max(1, target_pods // namespaces)

        return CL2PreloadPlan(
            target_pods=target_pods,
            namespaces=namespaces,
            deployments_per_ns=deployments_per_ns,
            pod_replicas=pod_replicas,
            services_per_ns=services_per_ns,
            configmaps_per_ns=configmaps_per_ns,
            secrets_per_ns=secrets_per_ns,
        )


@dataclass
class CL2TestStatus(_SerializableMixin):
    """Overall CL2 test execution status."""
    config_name: str
    job_name: str
    status: str
    duration_seconds: float
    error_message: Optional[str] = None


@dataclass
class APIAvailabilityResult(_SerializableMixin):
    """API server availability during CL2 test."""
    availability_percentage: float    # 0-100
    longest_unavailable_period: str   # e.g. "0s", "2.5s"


@dataclass
class CL2Summary(_SerializableMixin):
    """Complete CL2 preload test results."""
    test_status: CL2TestStatus
    preload_plan: Optional[CL2PreloadPlan]
    pod_startup_latencies: List[LatencyPercentile]
    api_latencies: List[APILatencyResult]
    api_availability: Optional[APIAvailabilityResult]
    scheduling_throughput: Optional[SchedulingThroughputResult]
    raw_results: Dict[str, Any]


class CL2ParseError(Exception):
    """Raised when CL2 JSON output cannot be parsed."""
    pass


# ---------------------------------------------------------------------------
# Flux / Deployment Models
# ---------------------------------------------------------------------------

@dataclass
class DeploymentConfig(_SerializableMixin):
    name: str
    namespace: str
    current_replicas: int
    resource_requests: Dict[str, Any]
    node_selector: Dict[str, Any]
    tolerations: List[Dict[str, Any]]
    affinity: Dict[str, Any]
    source_path: str
    role: Optional[str] = None
    weight: Optional[float] = None


@dataclass
class HelmReleaseConfig(_SerializableMixin):
    name: str
    namespace: str
    chart: str
    source_path: str


# ---------------------------------------------------------------------------
# Known Issues KB Models
# ---------------------------------------------------------------------------

@dataclass
class Signature(_SerializableMixin):
    event_reasons: List[str]
    log_patterns: List[str]
    metric_conditions: List[str]
    resource_kinds: List[str]


@dataclass
class AffectedVersions(_SerializableMixin):
    component: str
    min_version: Optional[str] = None
    max_version: Optional[str] = None
    fixed_in: Optional[str] = None


@dataclass
class KBEntry(_SerializableMixin):
    entry_id: str
    title: str
    category: str
    signature: Signature
    root_cause: str
    recommended_actions: List[str]
    severity: Severity
    affected_versions: List[AffectedVersions]
    created_at: datetime
    last_seen: datetime
    occurrence_count: int
    status: str = "active"
    review_notes: Optional[str] = None
    alternative_explanations: List[str] = field(default_factory=list)
    checkpoint_questions: List[str] = field(default_factory=list)
