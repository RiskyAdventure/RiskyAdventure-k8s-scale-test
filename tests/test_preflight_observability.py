"""Unit tests for PreflightChecker observability connectivity checks (AMP, CloudWatch, EKS).

Tests run the full ``PreflightChecker.run()`` method with all non-observability
dependencies mocked out, then assert on ``obs_passed``, ``obs_total``, and
the ``recommendations`` list in the returned ``PreflightReport``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from k8s_scale_test.models import (
    EC2Quotas,
    NodeClassConfig,
    NodePoolConfig,
    SubnetIPInfo,
    TestConfig,
)
from k8s_scale_test.preflight import PreflightChecker


# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------

def _minimal_ec2_quotas() -> EC2Quotas:
    """Return EC2Quotas with plenty of headroom so capacity checks pass."""
    return EC2Quotas(
        current_vcpu_usage=0,
        vcpu_quota=10_000,
        headroom_vcpus=10_000,
        usage_pct=0.0,
    )


def _minimal_subnets() -> list[SubnetIPInfo]:
    """Return a single subnet with plenty of IPs."""
    return [
        SubnetIPInfo(
            subnet_id="subnet-aaa",
            availability_zone="us-west-2a",
            available_ips=50_000,
            cidr_block="10.0.0.0/16",
        )
    ]


def _minimal_nodepools() -> list[NodePoolConfig]:
    """Return one NodePool with enough capacity for 100 pods."""
    return [
        NodePoolConfig(
            name="default",
            cpu_limit=1000,
            memory_limit_gi=2000,
            instance_types=["m7g.2xlarge"],
            architecture="arm64",
            capacity_type="on-demand",
            node_class_name="default",
        )
    ]


def _minimal_nodeclasses() -> list[NodeClassConfig]:
    """Return one NodeClass matching the default pool."""
    return [
        NodeClassConfig(
            name="default",
            max_pods=110,
            pods_per_core=2,
            system_reserved_cpu="100m",
            system_reserved_memory="100Mi",
            kube_reserved_cpu="100m",
            kube_reserved_memory="100Mi",
            eviction_hard_memory="100Mi",
            subnet_discovery_tag="karpenter.sh/discovery",
        )
    ]


def _make_checker(config: TestConfig) -> PreflightChecker:
    """Build a PreflightChecker with mocked aws_client and k8s_client."""
    checker = PreflightChecker(
        config=config,
        aws_client=MagicMock(),
        k8s_client=MagicMock(),
    )
    return checker


def _patch_internals(checker: PreflightChecker):
    """Return a list of patch context managers that mock out all non-observability
    async helpers so ``run()`` can complete without real API calls."""
    patches = [
        patch.object(checker, "_get_ec2_quotas", new_callable=AsyncMock, return_value=_minimal_ec2_quotas()),
        patch.object(checker, "_get_subnet_ip_availability", new_callable=AsyncMock, return_value=_minimal_subnets()),
        patch.object(checker, "_get_nodepool_configs", new_callable=AsyncMock, return_value=_minimal_nodepools()),
        patch.object(checker, "_get_nodeclass_configs", new_callable=AsyncMock, return_value=_minimal_nodeclasses()),
        patch.object(checker, "_count_daemonsets", new_callable=AsyncMock, return_value=2),
    ]
    return patches


async def _run_checker(checker: PreflightChecker):
    """Run the checker with all internal helpers mocked, plus FluxRepoReader."""
    patches = _patch_internals(checker)
    mock_reader = MagicMock()
    mock_reader.get_deployments.return_value = []

    with patch("k8s_scale_test.preflight.PreflightChecker._get_node_allocatable", new_callable=AsyncMock, return_value=None):
        for p in patches:
            p.start()
        try:
            with patch("k8s_scale_test.flux.FluxRepoReader", return_value=mock_reader):
                report = await checker.run()
        finally:
            for p in patches:
                p.stop()
    return report


# ---------------------------------------------------------------------------
# Task 2.2: AMP check passes
# ---------------------------------------------------------------------------

class TestAMPCheckPasses:
    """Requirement 1.1: AMP check passes when amp_workspace_id is set and
    _query_promql returns {"status": "success"}."""

    @pytest.mark.asyncio
    async def test_amp_success_increments_obs_passed(self):
        config = TestConfig(
            target_pods=100,
            amp_workspace_id="ws-abc123",
            flux_repo_path="/tmp/fake-flux",
        )
        checker = _make_checker(config)

        mock_collector = MagicMock()
        mock_collector._query_promql = AsyncMock(return_value={"status": "success"})

        with patch("k8s_scale_test.health_sweep.AMPMetricCollector", return_value=mock_collector):
            report = await _run_checker(checker)

        # AMP passed — obs_passed should include the AMP check
        assert report.obs_passed >= 1
        # No AMP-related recommendation
        amp_recs = [r for r in report.decision.recommendations if "AMP" in r]
        assert amp_recs == []


# ---------------------------------------------------------------------------
# Task 2.3: AMP check non-success status
# ---------------------------------------------------------------------------

class TestAMPCheckNonSuccess:
    """Requirement 1.2: AMP check adds recommendation when _query_promql
    returns a non-success status."""

    @pytest.mark.asyncio
    async def test_amp_non_success_adds_recommendation(self):
        config = TestConfig(
            target_pods=100,
            amp_workspace_id="ws-abc123",
            flux_repo_path="/tmp/fake-flux",
        )
        checker = _make_checker(config)

        mock_collector = MagicMock()
        mock_collector._query_promql = AsyncMock(return_value={"status": "error"})

        with patch("k8s_scale_test.health_sweep.AMPMetricCollector", return_value=mock_collector):
            report = await _run_checker(checker)

        amp_recs = [r for r in report.decision.recommendations if "AMP" in r]
        assert len(amp_recs) == 1
        assert "error" in amp_recs[0].lower() or "status" in amp_recs[0].lower()


# ---------------------------------------------------------------------------
# Task 2.4: AMP check exception
# ---------------------------------------------------------------------------

class TestAMPCheckException:
    """Requirement 1.3: AMP check adds recommendation when _query_promql
    raises an exception."""

    @pytest.mark.asyncio
    async def test_amp_exception_adds_recommendation(self):
        config = TestConfig(
            target_pods=100,
            amp_workspace_id="ws-abc123",
            flux_repo_path="/tmp/fake-flux",
        )
        checker = _make_checker(config)

        mock_collector = MagicMock()
        mock_collector._query_promql = AsyncMock(side_effect=ConnectionError("timeout"))

        with patch("k8s_scale_test.health_sweep.AMPMetricCollector", return_value=mock_collector):
            report = await _run_checker(checker)

        amp_recs = [r for r in report.decision.recommendations if "AMP" in r]
        assert len(amp_recs) == 1
        assert "failed" in amp_recs[0].lower() or "timeout" in amp_recs[0].lower()


# ---------------------------------------------------------------------------
# Task 2.5: AMP check skipped
# ---------------------------------------------------------------------------

class TestAMPCheckSkipped:
    """Requirement 1.4: AMP check skipped when neither amp_workspace_id
    nor prometheus_url is configured."""

    @pytest.mark.asyncio
    async def test_amp_skipped_adds_not_configured_recommendation(self):
        config = TestConfig(
            target_pods=100,
            amp_workspace_id=None,
            prometheus_url=None,
            flux_repo_path="/tmp/fake-flux",
        )
        checker = _make_checker(config)

        # No AMP patching needed — the code path skips AMP entirely
        report = await _run_checker(checker)

        amp_recs = [r for r in report.decision.recommendations
                    if "AMP" in r or "Prometheus" in r]
        assert len(amp_recs) == 1
        assert "not configured" in amp_recs[0].lower() or "no amp" in amp_recs[0].lower()


# ---------------------------------------------------------------------------
# Task 3.1: CloudWatch check passes
# ---------------------------------------------------------------------------

class TestCloudWatchCheckPasses:
    """Requirement 2.1: CloudWatch check passes when cloudwatch_log_group is set
    and describe_log_groups returns the matching group."""

    @pytest.mark.asyncio
    async def test_cloudwatch_success_increments_obs_passed(self):
        config = TestConfig(
            target_pods=100,
            cloudwatch_log_group="/aws/eks/my-cluster/logs",
            flux_repo_path="/tmp/fake-flux",
        )
        checker = _make_checker(config)

        # Mock the logs client returned by aws_client.client("logs", ...)
        mock_logs_client = MagicMock()
        mock_logs_client.describe_log_groups.return_value = {
            "logGroups": [
                {"logGroupName": "/aws/eks/my-cluster/logs"}
            ]
        }
        checker.aws_client.client.return_value = mock_logs_client

        report = await _run_checker(checker)

        # CloudWatch passed — obs_passed should include the CW check
        assert report.obs_passed >= 1
        # No CloudWatch-related recommendation
        cw_recs = [r for r in report.decision.recommendations if "CloudWatch" in r]
        assert cw_recs == []


# ---------------------------------------------------------------------------
# Task 3.2: CloudWatch check — log group not found
# ---------------------------------------------------------------------------

class TestCloudWatchCheckNotFound:
    """Requirement 2.2: CloudWatch check adds recommendation when log group
    not found in response."""

    @pytest.mark.asyncio
    async def test_cloudwatch_not_found_adds_recommendation(self):
        config = TestConfig(
            target_pods=100,
            cloudwatch_log_group="/aws/eks/my-cluster/logs",
            flux_repo_path="/tmp/fake-flux",
        )
        checker = _make_checker(config)

        # Return a different log group name so the exact match fails
        mock_logs_client = MagicMock()
        mock_logs_client.describe_log_groups.return_value = {
            "logGroups": [
                {"logGroupName": "/aws/eks/other-cluster/logs"}
            ]
        }
        checker.aws_client.client.return_value = mock_logs_client

        report = await _run_checker(checker)

        cw_recs = [r for r in report.decision.recommendations if "CloudWatch" in r]
        assert len(cw_recs) == 1
        assert "not found" in cw_recs[0].lower()


# ---------------------------------------------------------------------------
# Task 3.3: CloudWatch check — exception
# ---------------------------------------------------------------------------

class TestCloudWatchCheckException:
    """Requirement 2.3: CloudWatch check adds recommendation when
    describe_log_groups raises an exception."""

    @pytest.mark.asyncio
    async def test_cloudwatch_exception_adds_recommendation(self):
        config = TestConfig(
            target_pods=100,
            cloudwatch_log_group="/aws/eks/my-cluster/logs",
            flux_repo_path="/tmp/fake-flux",
        )
        checker = _make_checker(config)

        mock_logs_client = MagicMock()
        mock_logs_client.describe_log_groups.side_effect = ConnectionError("network timeout")
        checker.aws_client.client.return_value = mock_logs_client

        report = await _run_checker(checker)

        cw_recs = [r for r in report.decision.recommendations if "CloudWatch" in r]
        assert len(cw_recs) == 1
        assert "failed" in cw_recs[0].lower() or "timeout" in cw_recs[0].lower()


# ---------------------------------------------------------------------------
# Task 3.4: CloudWatch check skipped
# ---------------------------------------------------------------------------

class TestCloudWatchCheckSkipped:
    """Requirement 2.4: CloudWatch check skipped when cloudwatch_log_group
    is not configured."""

    @pytest.mark.asyncio
    async def test_cloudwatch_skipped_adds_not_configured_recommendation(self):
        config = TestConfig(
            target_pods=100,
            cloudwatch_log_group=None,
            flux_repo_path="/tmp/fake-flux",
        )
        checker = _make_checker(config)

        report = await _run_checker(checker)

        cw_recs = [r for r in report.decision.recommendations if "CloudWatch" in r]
        assert len(cw_recs) == 1
        assert "not configured" in cw_recs[0].lower() or "no cloudwatch" in cw_recs[0].lower()


# ---------------------------------------------------------------------------
# Task 4.1: EKS check passes endpoint_url when aws_endpoint_url is configured
# ---------------------------------------------------------------------------

class TestEKSCheckEndpointUrlPassed:
    """Requirement 3.1: EKS check passes endpoint_url to boto3 client when
    aws_endpoint_url is configured."""

    @pytest.mark.asyncio
    async def test_eks_passes_endpoint_url_when_configured(self):
        config = TestConfig(
            target_pods=100,
            eks_cluster_name="my-cluster",
            aws_endpoint_url="https://api.beta.us-west-2.wesley.amazonaws.com",
            flux_repo_path="/tmp/fake-flux",
        )
        checker = _make_checker(config)

        # The mock aws_client.client() returns a new mock each call.
        # We need the EKS mock to return a valid describe_cluster response.
        mock_eks_client = MagicMock()
        mock_eks_client.describe_cluster.return_value = {
            "cluster": {"status": "ACTIVE"}
        }

        def client_side_effect(service, **kwargs):
            if service == "eks":
                return mock_eks_client
            # Return a default mock for other services (e.g. "logs")
            return MagicMock()

        checker.aws_client.client.side_effect = client_side_effect

        report = await _run_checker(checker)

        # Find the EKS-specific call in call_args_list
        eks_calls = [
            call for call in checker.aws_client.client.call_args_list
            if call.args and call.args[0] == "eks"
        ]
        assert len(eks_calls) == 1
        _, eks_kwargs = eks_calls[0]
        assert "endpoint_url" in eks_kwargs
        assert eks_kwargs["endpoint_url"] == config.aws_endpoint_url


# ---------------------------------------------------------------------------
# Task 4.2: EKS check does NOT pass endpoint_url when aws_endpoint_url is empty/None
# ---------------------------------------------------------------------------

class TestEKSCheckNoEndpointUrl:
    """Requirement 3.2: EKS check does NOT pass endpoint_url when
    aws_endpoint_url is empty/None."""

    @pytest.mark.asyncio
    async def test_eks_no_endpoint_url_when_not_configured(self):
        config = TestConfig(
            target_pods=100,
            eks_cluster_name="my-cluster",
            aws_endpoint_url="",
            flux_repo_path="/tmp/fake-flux",
        )
        checker = _make_checker(config)

        mock_eks_client = MagicMock()
        mock_eks_client.describe_cluster.return_value = {
            "cluster": {"status": "ACTIVE"}
        }

        def client_side_effect(service, **kwargs):
            if service == "eks":
                return mock_eks_client
            return MagicMock()

        checker.aws_client.client.side_effect = client_side_effect

        report = await _run_checker(checker)

        # Find the EKS-specific call
        eks_calls = [
            call for call in checker.aws_client.client.call_args_list
            if call.args and call.args[0] == "eks"
        ]
        assert len(eks_calls) == 1
        _, eks_kwargs = eks_calls[0]
        assert "endpoint_url" not in eks_kwargs


# ---------------------------------------------------------------------------
# Task 4.3: EKS check passes when cluster status is "ACTIVE"
# ---------------------------------------------------------------------------

class TestEKSCheckActivePasses:
    """Requirement 3.3: EKS check passes when cluster status is ACTIVE."""

    @pytest.mark.asyncio
    async def test_eks_active_increments_obs_passed(self):
        config = TestConfig(
            target_pods=100,
            eks_cluster_name="my-cluster",
            flux_repo_path="/tmp/fake-flux",
        )
        checker = _make_checker(config)

        mock_eks_client = MagicMock()
        mock_eks_client.describe_cluster.return_value = {
            "cluster": {"status": "ACTIVE"}
        }

        def client_side_effect(service, **kwargs):
            if service == "eks":
                return mock_eks_client
            return MagicMock()

        checker.aws_client.client.side_effect = client_side_effect

        report = await _run_checker(checker)

        assert report.obs_passed >= 1
        eks_recs = [r for r in report.decision.recommendations if "EKS" in r]
        assert eks_recs == []


# ---------------------------------------------------------------------------
# Task 4.4: EKS check adds recommendation when cluster status is not "ACTIVE"
# ---------------------------------------------------------------------------

class TestEKSCheckNonActiveStatus:
    """Requirement 3.4: EKS check adds recommendation when cluster status
    is not ACTIVE."""

    @pytest.mark.asyncio
    async def test_eks_non_active_adds_recommendation(self):
        config = TestConfig(
            target_pods=100,
            eks_cluster_name="my-cluster",
            flux_repo_path="/tmp/fake-flux",
        )
        checker = _make_checker(config)

        mock_eks_client = MagicMock()
        mock_eks_client.describe_cluster.return_value = {
            "cluster": {"status": "CREATING"}
        }

        def client_side_effect(service, **kwargs):
            if service == "eks":
                return mock_eks_client
            return MagicMock()

        checker.aws_client.client.side_effect = client_side_effect

        report = await _run_checker(checker)

        eks_recs = [r for r in report.decision.recommendations if "EKS" in r]
        assert len(eks_recs) == 1
        assert "CREATING" in eks_recs[0]
        assert "not ACTIVE" in eks_recs[0] or "ACTIVE" in eks_recs[0]


# ---------------------------------------------------------------------------
# Task 4.5: EKS check adds recommendation when describe_cluster raises exception
# ---------------------------------------------------------------------------

class TestEKSCheckException:
    """Requirement 3.5: EKS check adds recommendation when describe_cluster
    raises an exception."""

    @pytest.mark.asyncio
    async def test_eks_exception_adds_recommendation(self):
        config = TestConfig(
            target_pods=100,
            eks_cluster_name="my-cluster",
            flux_repo_path="/tmp/fake-flux",
        )
        checker = _make_checker(config)

        mock_eks_client = MagicMock()
        mock_eks_client.describe_cluster.side_effect = ConnectionError("connection refused")

        def client_side_effect(service, **kwargs):
            if service == "eks":
                return mock_eks_client
            return MagicMock()

        checker.aws_client.client.side_effect = client_side_effect

        report = await _run_checker(checker)

        eks_recs = [r for r in report.decision.recommendations if "EKS" in r]
        assert len(eks_recs) == 1
        assert "failed" in eks_recs[0].lower() or "connection refused" in eks_recs[0].lower()


# ---------------------------------------------------------------------------
# Task 4.6: EKS check skipped when eks_cluster_name is not configured
# ---------------------------------------------------------------------------

class TestEKSCheckSkipped:
    """Requirement 3.6: EKS check skipped when eks_cluster_name is not configured."""

    @pytest.mark.asyncio
    async def test_eks_skipped_adds_not_configured_recommendation(self):
        config = TestConfig(
            target_pods=100,
            eks_cluster_name=None,
            flux_repo_path="/tmp/fake-flux",
        )
        checker = _make_checker(config)

        report = await _run_checker(checker)

        eks_recs = [r for r in report.decision.recommendations
                    if "EKS" in r or "eks" in r.lower()]
        assert len(eks_recs) == 1
        assert "not configured" in eks_recs[0].lower() or "no eks" in eks_recs[0].lower()
