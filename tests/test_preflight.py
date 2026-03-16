"""Unit tests for PreflightChecker._calculate_pods_per_node."""

from k8s_scale_test.models import NodeClassConfig, TestConfig, PodsPerNodeBreakdown
from k8s_scale_test.preflight import PreflightChecker, INSTANCE_TYPE_CORES


def _make_node_class(
    max_pods: int = 110,
    pods_per_core: int = 2,
    system_reserved_cpu: str = "100m",
    system_reserved_memory: str = "100Mi",
    kube_reserved_cpu: str = "100m",
    kube_reserved_memory: str = "100Mi",
    eviction_hard_memory: str = "100Mi",
) -> NodeClassConfig:
    return NodeClassConfig(
        name="test-class",
        max_pods=max_pods,
        pods_per_core=pods_per_core,
        system_reserved_cpu=system_reserved_cpu,
        system_reserved_memory=system_reserved_memory,
        kube_reserved_cpu=kube_reserved_cpu,
        kube_reserved_memory=kube_reserved_memory,
        eviction_hard_memory=eviction_hard_memory,
        subnet_discovery_tag="karpenter.sh/discovery",
    )


def _checker() -> PreflightChecker:
    return PreflightChecker(config=TestConfig(target_pods=1000))


# --- Core formula tests ---


class TestCalculatePodsPerNode:
    """Tests for _calculate_pods_per_node."""

    def test_c7g_2xlarge_maxpods_is_binding(self):
        """c7g.2xlarge: 8 cores * 2 podsPerCore = 16, maxPods=110 → 16."""
        checker = _checker()
        nc = _make_node_class(max_pods=110, pods_per_core=2)
        result = checker._calculate_pods_per_node("c7g.2xlarge", nc, "prod", 8)

        assert result.effective_max_pods == 16
        assert result.pods_per_core_limit == 16
        assert result.max_pods_setting == 110

    def test_i4i_8xlarge_maxpods_is_binding(self):
        """i4i.8xlarge: 32 cores * 2 podsPerCore = 64, maxPods=110 → 64."""
        checker = _checker()
        nc = _make_node_class(max_pods=110, pods_per_core=2)
        result = checker._calculate_pods_per_node("i4i.8xlarge", nc, "alt", 32)

        assert result.effective_max_pods == 64
        assert result.pods_per_core_limit == 64

    def test_pods_per_core_is_binding(self):
        """When podsPerCore limit < maxPods, podsPerCore wins."""
        checker = _checker()
        nc = _make_node_class(max_pods=200, pods_per_core=1)
        result = checker._calculate_pods_per_node("i4i.8xlarge", nc, "alt", 32)

        assert result.effective_max_pods == 32
        assert result.pods_per_core_limit == 32

    def test_equal_limits(self):
        """When both limits are equal, effective_max_pods equals both."""
        checker = _checker()
        nc = _make_node_class(max_pods=16, pods_per_core=2)
        result = checker._calculate_pods_per_node("c7g.2xlarge", nc, "prod", 8)

        assert result.effective_max_pods == 16
        assert result.pods_per_core_limit == 16

    def test_all_fields_populated(self):
        """All PodsPerNodeBreakdown fields are correctly populated."""
        checker = _checker()
        nc = _make_node_class(
            max_pods=110,
            pods_per_core=2,
            system_reserved_cpu="200m",
            system_reserved_memory="256Mi",
            kube_reserved_cpu="150m",
            kube_reserved_memory="512Mi",
            eviction_hard_memory="100Mi",
        )
        result = checker._calculate_pods_per_node("m7g.2xlarge", nc, "prod", 8)

        assert result.instance_type == "m7g.2xlarge"
        assert result.nodepool_name == "prod"
        assert result.total_cores == 8
        assert result.max_pods_setting == 110
        assert result.pods_per_core_setting == 2
        assert result.pods_per_core_limit == 16
        assert result.effective_max_pods == 16
        assert result.system_reserved_cpu == "200m"
        assert result.kube_reserved_cpu == "150m"
        assert result.system_reserved_memory == "256Mi"
        assert result.kube_reserved_memory == "512Mi"
        assert result.eviction_hard_memory == "100Mi"

    def test_returns_pods_per_node_breakdown_type(self):
        checker = _checker()
        nc = _make_node_class()
        result = checker._calculate_pods_per_node("c7g.2xlarge", nc, "prod", 8)
        assert isinstance(result, PodsPerNodeBreakdown)

    def test_instance_type_cores_lookup(self):
        """Verify the INSTANCE_TYPE_CORES lookup has expected entries."""
        assert INSTANCE_TYPE_CORES["c7g.2xlarge"] == 8
        assert INSTANCE_TYPE_CORES["m7g.2xlarge"] == 8
        assert INSTANCE_TYPE_CORES["i4i.8xlarge"] == 32
        assert "i7i.24xlarge" not in INSTANCE_TYPE_CORES
