"""Tests for proactive node health checks in anomaly detection.

Validates that:
1. SSM commands are only dispatched when evidence justifies them (no always-on)
2. PSI/kubelet/disk/mpstat parsers correctly extract issues from SSM output
3. _pick_ssm_commands is event-driven, not unconditional
4. SSM only targets suspect nodes, never random healthy nodes
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone
from unittest.mock import MagicMock

from k8s_scale_test.anomaly import AnomalyDetector
from k8s_scale_test.models import (
    NodeDiagnostic, SSMCommandResult, TestConfig,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> TestConfig:
    defaults = dict(
        target_pods=100, batch_size=10, batch_interval_seconds=5.0,
        rate_drop_threshold_pct=75.0, pending_timeout_seconds=300.0,
        rolling_avg_window_seconds=30.0, kubeconfig=None, aws_profile=None,
        aws_endpoint_url=None, flux_repo_path=".", output_dir=".",
        prometheus_url=None, event_time_window_minutes=5, ssm_log_lines=100,
        ssm_journal_minutes=5, auto_approve=True, exclude_apps=[],
        include_apps=[], cl2_preload=None, cl2_timeout=3600.0,
        cl2_params=None, hold_at_peak=90, stressor_weights={},
        cpu_limit_multiplier=2.0, memory_limit_multiplier=1.5,
        iperf3_server_ratio=50,
    )
    defaults.update(overrides)
    return TestConfig(**defaults)


def _make_ssm_result(output: str = "", status: str = "Success") -> SSMCommandResult:
    return SSMCommandResult(
        instance_id="i-test", command="test", command_id="cmd-1",
        status=status, output=output, error="",
    )


def _make_diag(resource_output: str = "") -> NodeDiagnostic:
    empty = _make_ssm_result("")
    return NodeDiagnostic(
        node_name="ip-10-0-1-1.ec2.internal",
        instance_id="i-test",
        collection_timestamp=datetime.now(timezone.utc),
        kubelet_logs=empty, containerd_logs=empty,
        journal_kubelet=empty, journal_containerd=empty,
        resource_utilization=_make_ssm_result(resource_output),
    )


def _make_detector() -> AnomalyDetector:
    config = _make_config()
    return AnomalyDetector(
        config=config, k8s_client=MagicMock(),
        node_metrics=MagicMock(), node_diag=MagicMock(),
        evidence_store=MagicMock(), run_id="test-run",
    )


# ===========================================================================
# Part 1: _pick_ssm_commands — event-driven, NOT always-on
# ===========================================================================

class TestPickSSMCommands:
    """SSM commands must only fire when K8s events or ENI evidence justify them.

    This is the critical scalability gate: at 200+ nodes, unconditional SSM
    commands would generate thousands of API calls per alert.
    """

    def test_no_warnings_no_extra_commands(self):
        """Zero warning events + zero ENI issues = zero extra SSM commands."""
        det = _make_detector()
        cmds = det._pick_ssm_commands({}, {})
        assert cmds == {}, f"Expected empty, got {list(cmds.keys())}"

    def test_no_psi_without_memory_events(self):
        """PSI only fires on Evicted/OOMKilling — not on scheduling failures."""
        det = _make_detector()
        cmds = det._pick_ssm_commands({"FailedScheduling": 5}, {})
        assert "psi" not in cmds

    def test_psi_added_on_eviction(self):
        det = _make_detector()
        cmds = det._pick_ssm_commands({"Evicted": 3}, {})
        assert "psi" in cmds
        assert "/proc/pressure" in cmds["psi"]

    def test_psi_added_on_oom(self):
        det = _make_detector()
        cmds = det._pick_ssm_commands({"OOMKilling": 1}, {})
        assert "psi" in cmds

    def test_no_kubelet_health_without_notready(self):
        det = _make_detector()
        cmds = det._pick_ssm_commands({"FailedScheduling": 10}, {})
        assert "kubelet_health" not in cmds

    def test_kubelet_health_on_notready(self):
        det = _make_detector()
        cmds = det._pick_ssm_commands({"NodeNotReady": 2}, {})
        assert "kubelet_health" in cmds
        assert "healthz" in cmds["kubelet_health"]

    def test_no_disk_readiness_without_invaliddisk(self):
        det = _make_detector()
        cmds = det._pick_ssm_commands({"FailedCreatePodSandBox": 5}, {})
        assert "disk_readiness" not in cmds

    def test_disk_readiness_on_invaliddisk(self):
        det = _make_detector()
        cmds = det._pick_ssm_commands({"InvalidDiskCapacity": 50}, {})
        assert "disk_readiness" in cmds
        assert "nvme" in cmds["disk_readiness"]

    def test_mpstat_as_fallback_for_unknown_warnings(self):
        """mpstat fires as fallback when warnings exist but no specific handler matched."""
        det = _make_detector()
        cmds = det._pick_ssm_commands({"SomeUnknownReason": 5}, {})
        assert "mpstat" in cmds

    def test_no_mpstat_when_specific_cause_found(self):
        """mpstat should NOT fire when a specific handler already matched."""
        det = _make_detector()
        cmds = det._pick_ssm_commands({"FailedCreatePodSandBox": 5}, {})
        assert "mpstat" not in cmds

    def test_mpstat_on_eviction(self):
        det = _make_detector()
        cmds = det._pick_ssm_commands({"Evicted": 2}, {})
        assert "mpstat" in cmds

    def test_ipamd_on_zero_prefix(self):
        det = _make_detector()
        eni = {"node-1": {"prefix_count": 0}}
        cmds = det._pick_ssm_commands({}, eni)
        assert "ipamd_log" in cmds

    def test_worst_case_command_count_bounded(self):
        """Even with every trigger active, extra commands stay bounded."""
        det = _make_detector()
        all_warnings = {
            "FailedCreatePodSandBox": 10, "Failed": 5, "ErrImagePull": 3,
            "Evicted": 2, "OOMKilling": 1, "FailedScheduling": 5,
            "NodeNotReady": 3, "InvalidDiskCapacity": 50,
        }
        eni = {"node-1": {"prefix_count": 0}}
        cmds = det._pick_ssm_commands(all_warnings, eni)
        assert len(cmds) <= 12, f"Too many extra commands: {len(cmds)}"


# ===========================================================================
# Part 2: PSI parser
# ===========================================================================

class TestParsePSI:

    def test_high_cpu_pressure_detected(self):
        det = _make_detector()
        output = (
            "=== psi ===\n"
            "some avg10=30.00 avg60=10.00 avg300=5.00 total=100\n"
            "===PSI_SEP===\n"
            "some avg10=2.00 avg60=1.00 avg300=0.50 total=50\n"
            "===PSI_SEP===\n"
            "some avg10=1.00 avg60=0.50 avg300=0.25 total=25\n"
        )
        diag = _make_diag(output)
        clues = det._parse_psi_evidence(diag)
        assert any("CPU pressure" in c for c in clues)
        assert not any("Memory" in c for c in clues)
        assert not any("IO" in c for c in clues)

    def test_high_io_pressure_detected(self):
        det = _make_detector()
        output = (
            "some avg10=1.00 avg60=0.50 avg300=0.25 total=10\n"
            "===PSI_SEP===\n"
            "some avg10=2.00 avg60=1.00 avg300=0.50 total=20\n"
            "===PSI_SEP===\n"
            "some avg10=15.00 avg60=8.00 avg300=4.00 total=100\n"
        )
        diag = _make_diag(output)
        clues = det._parse_psi_evidence(diag)
        assert any("IO pressure" in c for c in clues)

    def test_all_healthy_no_clues(self):
        det = _make_detector()
        output = (
            "some avg10=0.50 avg60=0.25 avg300=0.10 total=5\n"
            "===PSI_SEP===\n"
            "some avg10=1.00 avg60=0.50 avg300=0.25 total=10\n"
            "===PSI_SEP===\n"
            "some avg10=0.25 avg60=0.10 avg300=0.05 total=2\n"
        )
        diag = _make_diag(output)
        assert det._parse_psi_evidence(diag) == []

    def test_empty_output(self):
        det = _make_detector()
        assert det._parse_psi_evidence(_make_diag("")) == []

    def test_no_psi_separator(self):
        det = _make_detector()
        assert det._parse_psi_evidence(_make_diag("random text")) == []


# ===========================================================================
# Part 3: Kubelet health parser
# ===========================================================================

class TestParseKubeletHealth:

    def test_healthy(self):
        det = _make_detector()
        diag = _make_diag("ok===HEALTH_SEP===active")
        assert det._parse_kubelet_health_evidence(diag) == []

    def test_healthz_failed(self):
        det = _make_detector()
        diag = _make_diag("healthz check failed===HEALTH_SEP===active")
        clues = det._parse_kubelet_health_evidence(diag)
        assert any("healthz failed" in c for c in clues)

    def test_kubelet_inactive(self):
        det = _make_detector()
        diag = _make_diag("ok===HEALTH_SEP===inactive")
        clues = det._parse_kubelet_health_evidence(diag)
        assert any("not active" in c for c in clues)

    def test_no_separator_no_clues(self):
        det = _make_detector()
        assert det._parse_kubelet_health_evidence(_make_diag("random")) == []


# ===========================================================================
# Part 4: Disk readiness parser
# ===========================================================================

class TestParseDiskReadiness:

    def test_healthy_disk(self):
        det = _make_detector()
        output = "nvme0n1  259:0  0  100G  0 disk\n---\n/dev/nvme0n1p1  100G  20G  80G  20%  /\n"
        assert det._parse_disk_readiness_evidence(_make_diag(output)) == []

    def test_zero_capacity(self):
        det = _make_detector()
        output = "nvme0n1  259:0  0  0  0 disk\n---\n/dev/nvme0n1p1  0  0  0  0%  /\n"
        clues = det._parse_disk_readiness_evidence(_make_diag(output))
        assert any("0 capacity" in c for c in clues)

    def test_no_nvme_no_clues(self):
        det = _make_detector()
        assert det._parse_disk_readiness_evidence(_make_diag("unrelated")) == []


# ===========================================================================
# Part 5: mpstat parser
# ===========================================================================

class TestParseMpstat:

    def test_all_cores_healthy(self):
        det = _make_detector()
        output = (
            "01:00:01 AM  0  5.00  0.00  2.00  0.00  0.00  0.00  0.00  0.00  0.00  93.00\n"
            "01:00:01 AM  1  8.00  0.00  3.00  0.00  0.00  0.00  0.00  0.00  0.00  89.00\n"
        )
        assert det._parse_mpstat_evidence(_make_diag(output)) == []

    def test_saturated_core_detected(self):
        det = _make_detector()
        output = (
            "01:00:01 AM  0  85.00  0.00  10.00  0.00  0.00  0.00  0.00  0.00  0.00  5.00\n"
            "01:00:01 AM  1  2.00   0.00  1.00   0.00  0.00  0.00  0.00  0.00  0.00  97.00\n"
        )
        clues = det._parse_mpstat_evidence(_make_diag(output))
        assert len(clues) == 1
        assert "saturated" in clues[0].lower()

    def test_no_mpstat_output(self):
        det = _make_detector()
        assert det._parse_mpstat_evidence(_make_diag("NO_MPSTAT")) == []

    def test_multiple_saturated_cores(self):
        det = _make_detector()
        lines = []
        for i in range(8):
            idle = 5.0 if i < 3 else 90.0
            lines.append(f"01:00:01 AM  {i}  80.00  0.00  10.00  0.00  0.00  0.00  0.00  0.00  0.00  {idle:.2f}")
        clues = det._parse_mpstat_evidence(_make_diag("\n".join(lines)))
        assert len(clues) == 1
        assert "0,1,2" in clues[0]


# ===========================================================================
# Part 6: SSM call budget — structural audit
# ===========================================================================

class TestSSMCallBudget:
    """Structural tests that verify SSM usage stays bounded.

    At 200+ nodes, SSM is the scalability bottleneck. These tests verify
    the code structure enforces limits, not just current behavior.
    """

    def test_handle_alert_limits_ssm_to_3_nodes(self):
        """handle_alert must cap SSM targets at 3 nodes."""
        det = _make_detector()
        source = inspect.getsource(det.handle_alert)
        assert "targets[:3]" in source, "SSM target limit removed from handle_alert"

    def test_base_ssm_commands_unchanged(self):
        """diagnostics.collect() must have exactly 5 base commands."""
        from k8s_scale_test.diagnostics import NodeDiagnosticsCollector
        source = inspect.getsource(NodeDiagnosticsCollector.collect)
        keys = re.findall(r'^\s+"(\w+)":\s', source, re.MULTILINE)
        base_keys = {"kubelet_logs", "containerd_logs", "journal_kubelet",
                     "journal_containerd", "resource_utilization"}
        assert set(keys[:5]) == base_keys, f"Base commands changed: {set(keys[:5])}"

    def test_health_sweep_uses_single_command(self):
        """The SSM fallback sweep must use run_single_command (1 SSM call per node)."""
        from k8s_scale_test.health_sweep import SSMFallbackCollector
        source = inspect.getsource(SSMFallbackCollector.collect)
        assert "run_single_command" in source, "SSM fallback should use single SSM command"

    def test_health_sweep_is_separate_module(self):
        """Health sweep must be in its own module, not in controller."""
        from k8s_scale_test.controller import ScaleTestController
        assert not hasattr(ScaleTestController, '_run_node_health_sweep')
        assert not hasattr(ScaleTestController, '_parse_sweep_output')

    def test_karpenter_check_is_separate_module(self):
        """Karpenter check must be in its own module, not in controller."""
        from k8s_scale_test.controller import ScaleTestController
        assert not hasattr(ScaleTestController, '_check_karpenter_health')

    def test_ssm_only_on_suspect_nodes_in_reactive_path(self):
        """handle_alert only SSMs nodes from _merge_targets (stuck pods + bad conditions)."""
        det = _make_detector()
        source = inspect.getsource(det.handle_alert)
        assert "for node_name, iid in targets" in source


# ===========================================================================
# Part 7: Sweep output parser (now in health_sweep module)
# ===========================================================================

class TestSweepOutputParser:

    def test_all_healthy(self):
        from k8s_scale_test.health_sweep import parse_sweep_output
        output = (
            "===PSI_START===\n"
            "some avg10=0.50 avg60=0.25 avg300=0.10 total=5\n"
            "===PSI_SEP===\n"
            "some avg10=1.00 avg60=0.50 avg300=0.25 total=10\n"
            "===PSI_SEP===\n"
            "some avg10=0.25 avg60=0.10 avg300=0.05 total=2\n"
            "===KUBELET_START===\n"
            "active\n"
            "===DISK_START===\n"
            "/dev/nvme0n1p1  100G  20G  80G  20%  /\n"
            "===MPSTAT_START===\n"
            "01:00:01 AM  0  5.00  0.00  2.00  0.00  0.00  0.00  0.00  0.00  0.00  93.00\n"
        )
        assert parse_sweep_output("node-1", output) == []

    def test_high_cpu_pressure(self):
        from k8s_scale_test.health_sweep import parse_sweep_output
        output = (
            "===PSI_START===\n"
            "some avg10=40.00 avg60=20.00 avg300=10.00 total=500\n"
            "===PSI_SEP===\n"
            "some avg10=1.00 avg60=0.50 avg300=0.25 total=10\n"
            "===PSI_SEP===\n"
            "some avg10=0.25 avg60=0.10 avg300=0.05 total=2\n"
            "===KUBELET_START===\nactive\n"
            "===DISK_START===\n/dev/x  100G  20G  80G  20%  /\n"
            "===MPSTAT_START===\nNO_MPSTAT\n"
        )
        issues = parse_sweep_output("node-1", output)
        assert len(issues) == 1
        assert "CPU pressure" in issues[0]

    def test_kubelet_unhealthy(self):
        from k8s_scale_test.health_sweep import parse_sweep_output
        output = (
            "===PSI_START===\nsome avg10=0.5 avg60=0.2 avg300=0.1 total=5\n"
            "===PSI_SEP===\nsome avg10=1.0 avg60=0.5 avg300=0.2 total=10\n"
            "===PSI_SEP===\nsome avg10=0.2 avg60=0.1 avg300=0.0 total=2\n"
            "===KUBELET_START===\ninactive\n"
            "===DISK_START===\n/dev/x  100G  20G  80G  20%  /\n"
            "===MPSTAT_START===\nNO_MPSTAT\n"
        )
        issues = parse_sweep_output("node-1", output)
        assert any("Kubelet not active" in i for i in issues)

    def test_empty_output(self):
        from k8s_scale_test.health_sweep import parse_sweep_output
        assert parse_sweep_output("node-1", "") == []


# ===========================================================================
# Part 8: Infra health agent
# ===========================================================================

class TestInfraHealthAgent:

    def test_check_if_needed_skips_without_ice(self):
        """Should return empty dict when no InsufficientCapacityError findings."""
        import asyncio
        from k8s_scale_test.infra_health import InfraHealthAgent
        agent = InfraHealthAgent(MagicMock())
        result = asyncio.get_event_loop().run_until_complete(agent.check_if_needed([]))
        assert result == {}

    def test_parse_cpu_millicores(self):
        from k8s_scale_test.infra_health import _parse_cpu_millicores
        assert _parse_cpu_millicores("500m") == 500
        assert _parse_cpu_millicores("1") == 1000
        assert _parse_cpu_millicores("") == 0
        assert _parse_cpu_millicores("100000000n") == 100

    def test_parse_memory_mi(self):
        from k8s_scale_test.infra_health import _parse_memory_mi
        assert _parse_memory_mi("512Mi") == 512
        assert _parse_memory_mi("1Gi") == 1024
        assert _parse_memory_mi("524288Ki") == 512
        assert _parse_memory_mi("") == 0
