"""Unit tests for health_sweep threshold logic."""

from unittest.mock import MagicMock, patch
import asyncio

from k8s_scale_test.health_sweep import (
    K8sConditionChecker,
    NodeConditionResult,
    NodeMetricResult,
    check_threshold,
    parse_promql_response,
)


class TestCheckThreshold:
    """Tests for check_threshold()."""

    def test_cpu_above_threshold(self):
        r = NodeMetricResult("node-1", "cpu", 91.0)
        assert check_threshold(r) == "High CPU utilization 91.0% on node-1"

    def test_cpu_at_threshold(self):
        r = NodeMetricResult("node-1", "cpu", 90.0)
        assert check_threshold(r) is None

    def test_memory_above_threshold(self):
        r = NodeMetricResult("node-2", "memory", 95.3)
        assert check_threshold(r) == "High memory usage 95.3% on node-2"

    def test_memory_at_threshold(self):
        r = NodeMetricResult("node-2", "memory", 90.0)
        assert check_threshold(r) is None

    def test_disk_above_threshold(self):
        r = NodeMetricResult("node-3", "disk", 86.0)
        assert check_threshold(r) == "High disk usage 86.0% on node-3"

    def test_disk_at_threshold(self):
        r = NodeMetricResult("node-3", "disk", 85.0)
        assert check_threshold(r) is None

    def test_network_errors_above_zero(self):
        r = NodeMetricResult("node-4", "network_errors", 0.5)
        assert check_threshold(r) == "Network errors 0.5/s on node-4"

    def test_network_errors_at_zero(self):
        r = NodeMetricResult("node-4", "network_errors", 0.0)
        assert check_threshold(r) is None

    def test_pod_restarts_above_threshold(self):
        r = NodeMetricResult("node-5", "pod_restarts", 8.0)
        assert check_threshold(r) == "Pod restarts 8 in 15m on node-5"

    def test_pod_restarts_at_threshold(self):
        r = NodeMetricResult("node-5", "pod_restarts", 5.0)
        assert check_threshold(r) is None

    def test_unknown_category_returns_none(self):
        r = NodeMetricResult("node-6", "unknown_metric", 999.0)
        assert check_threshold(r) is None

    def test_below_threshold_returns_none(self):
        r = NodeMetricResult("node-7", "cpu", 10.0)
        assert check_threshold(r) is None


class TestParsePromqlResponse:
    """Tests for parse_promql_response()."""

    def _make_response(self, results):
        return {
            "status": "success",
            "data": {"resultType": "vector", "result": results},
        }

    def test_valid_single_node(self):
        resp = self._make_response([
            {"metric": {"node": "ip-10-0-1-100.ec2.internal"}, "value": [1234567890.123, "85.5"]},
        ])
        out = parse_promql_response(resp, "cpu")
        assert len(out) == 1
        assert out[0].node_name == "ip-10-0-1-100.ec2.internal"
        assert out[0].metric_category == "cpu"
        assert out[0].value == 85.5

    def test_valid_multiple_nodes(self):
        resp = self._make_response([
            {"metric": {"node": "node-a"}, "value": [0, "10.0"]},
            {"metric": {"node": "node-b"}, "value": [0, "20.0"]},
        ])
        out = parse_promql_response(resp, "memory")
        assert len(out) == 2
        assert out[0].node_name == "node-a"
        assert out[1].value == 20.0

    def test_instance_fallback(self):
        resp = self._make_response([
            {"metric": {"instance": "10.0.1.5:9100"}, "value": [0, "42.0"]},
        ])
        out = parse_promql_response(resp, "disk")
        assert len(out) == 1
        assert out[0].node_name == "10.0.1.5:9100"

    def test_node_preferred_over_instance(self):
        resp = self._make_response([
            {"metric": {"node": "real-node", "instance": "fallback"}, "value": [0, "1.0"]},
        ])
        out = parse_promql_response(resp, "cpu")
        assert out[0].node_name == "real-node"

    def test_empty_result_list(self):
        resp = self._make_response([])
        assert parse_promql_response(resp, "cpu") == []

    def test_non_success_status(self):
        resp = {"status": "error", "data": {"resultType": "vector", "result": []}}
        assert parse_promql_response(resp, "cpu") == []

    def test_wrong_result_type(self):
        resp = {"status": "success", "data": {"resultType": "matrix", "result": []}}
        assert parse_promql_response(resp, "cpu") == []

    def test_missing_data_key(self):
        assert parse_promql_response({"status": "success"}, "cpu") == []

    def test_not_a_dict(self):
        assert parse_promql_response("garbage", "cpu") == []
        assert parse_promql_response(None, "cpu") == []
        assert parse_promql_response(42, "cpu") == []

    def test_malformed_entry_skipped(self):
        resp = self._make_response([
            {"metric": {"node": "good"}, "value": [0, "5.0"]},
            {"metric": {}, "value": [0, "5.0"]},  # no node or instance
            {"metric": {"node": "also-good"}, "value": [0, "6.0"]},
        ])
        out = parse_promql_response(resp, "cpu")
        assert len(out) == 2
        assert out[0].node_name == "good"
        assert out[1].node_name == "also-good"

    def test_non_numeric_value_skipped(self):
        resp = self._make_response([
            {"metric": {"node": "n1"}, "value": [0, "NaN"]},
        ])
        # float("NaN") is valid in Python, so this should parse
        out = parse_promql_response(resp, "cpu")
        assert len(out) == 1

    def test_value_not_parseable_skipped(self):
        resp = self._make_response([
            {"metric": {"node": "n1"}, "value": [0, "not_a_number"]},
        ])
        assert parse_promql_response(resp, "cpu") == []

    def test_short_value_array_skipped(self):
        resp = self._make_response([
            {"metric": {"node": "n1"}, "value": [0]},
        ])
        assert parse_promql_response(resp, "cpu") == []



# ---------------------------------------------------------------------------
# Helpers for K8sConditionChecker tests
# ---------------------------------------------------------------------------

def _make_condition(cond_type: str, status: str, reason: str = "SomeReason"):
    """Create a mock K8s node condition."""
    cond = MagicMock()
    cond.type = cond_type
    cond.status = status
    cond.reason = reason
    return cond


def _make_node(name: str, conditions):
    """Create a mock K8s node with the given conditions."""
    node = MagicMock()
    node.metadata.name = name
    node.status.conditions = conditions
    return node


def _make_k8s_client(nodes):
    """Create a mock k8s client whose CoreV1Api().list_node() returns the given nodes."""
    client = MagicMock()
    node_list = MagicMock()
    node_list.items = nodes
    client.CoreV1Api.return_value.list_node.return_value = node_list
    return client


class TestK8sConditionChecker:
    """Tests for K8sConditionChecker condition classification logic."""

    def _run(self, checker):
        return asyncio.get_event_loop().run_until_complete(checker.check_all())

    def test_healthy_node_not_in_results(self):
        """A node with Ready=True and no pressure should not appear."""
        nodes = [_make_node("healthy-node", [
            _make_condition("Ready", "True"),
            _make_condition("DiskPressure", "False"),
            _make_condition("MemoryPressure", "False"),
            _make_condition("PIDPressure", "False"),
        ])]
        checker = K8sConditionChecker(_make_k8s_client(nodes))
        results = self._run(checker)
        assert results == []

    def test_not_ready_node(self):
        """A node with Ready != True should be flagged."""
        nodes = [_make_node("bad-node", [
            _make_condition("Ready", "False", "KubeletNotReady"),
            _make_condition("DiskPressure", "False"),
            _make_condition("MemoryPressure", "False"),
            _make_condition("PIDPressure", "False"),
        ])]
        checker = K8sConditionChecker(_make_k8s_client(nodes))
        results = self._run(checker)
        assert len(results) == 1
        assert results[0].node_name == "bad-node"
        assert "NotReady: KubeletNotReady" in results[0].issues

    def test_disk_pressure(self):
        nodes = [_make_node("disk-node", [
            _make_condition("Ready", "True"),
            _make_condition("DiskPressure", "True", "DiskPressureExists"),
            _make_condition("MemoryPressure", "False"),
            _make_condition("PIDPressure", "False"),
        ])]
        checker = K8sConditionChecker(_make_k8s_client(nodes))
        results = self._run(checker)
        assert len(results) == 1
        assert "DiskPressure: DiskPressureExists" in results[0].issues

    def test_memory_pressure(self):
        nodes = [_make_node("mem-node", [
            _make_condition("Ready", "True"),
            _make_condition("DiskPressure", "False"),
            _make_condition("MemoryPressure", "True", "MemoryPressureExists"),
            _make_condition("PIDPressure", "False"),
        ])]
        checker = K8sConditionChecker(_make_k8s_client(nodes))
        results = self._run(checker)
        assert len(results) == 1
        assert "MemoryPressure: MemoryPressureExists" in results[0].issues

    def test_pid_pressure(self):
        nodes = [_make_node("pid-node", [
            _make_condition("Ready", "True"),
            _make_condition("DiskPressure", "False"),
            _make_condition("MemoryPressure", "False"),
            _make_condition("PIDPressure", "True", "PIDPressureExists"),
        ])]
        checker = K8sConditionChecker(_make_k8s_client(nodes))
        results = self._run(checker)
        assert len(results) == 1
        assert "PIDPressure: PIDPressureExists" in results[0].issues

    def test_multiple_issues_on_one_node(self):
        """A node with multiple problems should have all issues listed."""
        nodes = [_make_node("multi-issue", [
            _make_condition("Ready", "False", "KubeletNotReady"),
            _make_condition("DiskPressure", "True", "DiskFull"),
            _make_condition("MemoryPressure", "True", "MemLow"),
            _make_condition("PIDPressure", "False"),
        ])]
        checker = K8sConditionChecker(_make_k8s_client(nodes))
        results = self._run(checker)
        assert len(results) == 1
        assert len(results[0].issues) == 3
        assert "NotReady: KubeletNotReady" in results[0].issues
        assert "DiskPressure: DiskFull" in results[0].issues
        assert "MemoryPressure: MemLow" in results[0].issues

    def test_mixed_healthy_and_unhealthy(self):
        """Only unhealthy nodes should appear in results."""
        nodes = [
            _make_node("good-1", [
                _make_condition("Ready", "True"),
                _make_condition("DiskPressure", "False"),
                _make_condition("MemoryPressure", "False"),
                _make_condition("PIDPressure", "False"),
            ]),
            _make_node("bad-1", [
                _make_condition("Ready", "False", "NotReady"),
                _make_condition("DiskPressure", "False"),
                _make_condition("MemoryPressure", "False"),
                _make_condition("PIDPressure", "False"),
            ]),
            _make_node("good-2", [
                _make_condition("Ready", "True"),
                _make_condition("DiskPressure", "False"),
                _make_condition("MemoryPressure", "False"),
                _make_condition("PIDPressure", "False"),
            ]),
        ]
        checker = K8sConditionChecker(_make_k8s_client(nodes))
        results = self._run(checker)
        assert len(results) == 1
        assert results[0].node_name == "bad-1"

    def test_empty_node_list(self):
        checker = K8sConditionChecker(_make_k8s_client([]))
        results = self._run(checker)
        assert results == []

    def test_k8s_api_failure_returns_empty(self):
        """K8s API failure should return empty list, not raise."""
        client = MagicMock()
        client.CoreV1Api.return_value.list_node.side_effect = Exception("API down")
        checker = K8sConditionChecker(client)
        results = self._run(checker)
        assert results == []

    def test_none_conditions_treated_as_empty(self):
        """A node with conditions=None should not crash."""
        node = MagicMock()
        node.metadata.name = "null-conds"
        node.status.conditions = None
        checker = K8sConditionChecker(_make_k8s_client([node]))
        results = self._run(checker)
        assert results == []

    def test_unknown_condition_type_ignored(self):
        """Condition types not in CONDITION_TYPES should be ignored."""
        nodes = [_make_node("extra-cond", [
            _make_condition("Ready", "True"),
            _make_condition("NetworkUnavailable", "True", "NoRouteCreated"),
            _make_condition("DiskPressure", "False"),
            _make_condition("MemoryPressure", "False"),
            _make_condition("PIDPressure", "False"),
        ])]
        checker = K8sConditionChecker(_make_k8s_client(nodes))
        results = self._run(checker)
        assert results == []

    def test_ready_unknown_status_flagged(self):
        """Ready with status 'Unknown' should be flagged as NotReady."""
        nodes = [_make_node("unknown-node", [
            _make_condition("Ready", "Unknown", "NodeStatusUnknown"),
        ])]
        checker = K8sConditionChecker(_make_k8s_client(nodes))
        results = self._run(checker)
        assert len(results) == 1
        assert "NotReady: NodeStatusUnknown" in results[0].issues

    def test_no_reason_uses_unknown(self):
        """When reason is None, 'Unknown' should be used."""
        cond = _make_condition("Ready", "False", None)
        cond.reason = None
        nodes = [_make_node("no-reason", [cond])]
        checker = K8sConditionChecker(_make_k8s_client(nodes))
        results = self._run(checker)
        assert len(results) == 1
        assert "NotReady: Unknown" in results[0].issues
