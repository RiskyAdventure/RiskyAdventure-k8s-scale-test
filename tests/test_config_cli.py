"""Tests for observability CLI arguments and TestConfig fields (Task 1)."""

from k8s_scale_test.cli import parse_args
from k8s_scale_test.models import TestConfig


class TestConfigObservabilityFields:
    """Verify new optional fields on TestConfig default to None."""

    def test_defaults_to_none(self):
        config = TestConfig(target_pods=100)
        assert config.amp_workspace_id is None
        assert config.cloudwatch_log_group is None
        assert config.eks_cluster_name is None

    def test_set_all_fields(self):
        config = TestConfig(
            target_pods=100,
            amp_workspace_id="ws-abc123",
            cloudwatch_log_group="/eks/cluster/nodes",
            eks_cluster_name="my-cluster",
        )
        assert config.amp_workspace_id == "ws-abc123"
        assert config.cloudwatch_log_group == "/eks/cluster/nodes"
        assert config.eks_cluster_name == "my-cluster"

    def test_serialization_round_trip(self):
        config = TestConfig(
            target_pods=100,
            amp_workspace_id="ws-abc123",
            cloudwatch_log_group="/eks/cluster/nodes",
            eks_cluster_name="my-cluster",
        )
        d = config.to_dict()
        restored = TestConfig.from_dict(d)
        assert restored.amp_workspace_id == "ws-abc123"
        assert restored.cloudwatch_log_group == "/eks/cluster/nodes"
        assert restored.eks_cluster_name == "my-cluster"

    def test_serialization_round_trip_none_fields(self):
        config = TestConfig(target_pods=100)
        d = config.to_dict()
        restored = TestConfig.from_dict(d)
        assert restored.amp_workspace_id is None
        assert restored.cloudwatch_log_group is None
        assert restored.eks_cluster_name is None


class TestCLIObservabilityArgs:
    """Verify new CLI arguments are parsed and default correctly."""

    def test_defaults_to_none(self):
        args = parse_args(["--target-pods", "500"])
        assert args.amp_workspace_id is None
        assert args.cloudwatch_log_group is None
        assert args.eks_cluster_name is None

    def test_all_args_provided(self):
        args = parse_args([
            "--target-pods", "500",
            "--amp-workspace-id", "ws-abc123",
            "--cloudwatch-log-group", "/eks/cluster/nodes",
            "--eks-cluster-name", "my-cluster",
        ])
        assert args.amp_workspace_id == "ws-abc123"
        assert args.cloudwatch_log_group == "/eks/cluster/nodes"
        assert args.eks_cluster_name == "my-cluster"

    def test_partial_args(self):
        args = parse_args([
            "--target-pods", "500",
            "--eks-cluster-name", "my-cluster",
        ])
        assert args.amp_workspace_id is None
        assert args.cloudwatch_log_group is None
        assert args.eks_cluster_name == "my-cluster"
