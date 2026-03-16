"""Unit tests for CL2ResultParser."""

import json
import pytest

from k8s_scale_test.cl2_parser import CL2ResultParser
from k8s_scale_test.models import CL2ParseError


SAMPLE_CL2_JSON = json.dumps({
    "version": "v1",
    "dataItems": [
        {
            "data": {"Perc50": 1234.5, "Perc90": 2345.6, "Perc99": 5678.9},
            "unit": "ms",
            "labels": {"Metric": "pod_startup_latency"},
        },
        {
            "data": {"Perc50": 10.2, "Perc90": 25.4, "Perc99": 100.1},
            "unit": "ms",
            "labels": {
                "Metric": "api_responsiveness",
                "Verb": "GET",
                "Resource": "pods",
                "Scope": "namespace",
                "Count": "1500",
            },
        },
        {
            "data": {"Perc50": 45.2, "Perc90": 45.2, "Perc99": 45.2},
            "unit": "1/s",
            "labels": {"Metric": "scheduling_throughput"},
        },
    ],
})


class TestParse:
    def test_parses_full_sample(self):
        summary = CL2ResultParser.parse(SAMPLE_CL2_JSON)
        assert summary.test_status.status == "Parsed"
        assert summary.test_status.config_name == "unknown"
        assert len(summary.pod_startup_latencies) == 3
        assert len(summary.api_latencies) == 1
        assert summary.scheduling_throughput is not None

    def test_raw_results_preserved(self):
        summary = CL2ResultParser.parse(SAMPLE_CL2_JSON)
        assert summary.raw_results["version"] == "v1"
        assert len(summary.raw_results["dataItems"]) == 3

    def test_invalid_json_raises_parse_error(self):
        with pytest.raises(CL2ParseError, match="Invalid JSON"):
            CL2ResultParser.parse("not json at all")

    def test_missing_data_items_raises_parse_error(self):
        with pytest.raises(CL2ParseError, match="Missing required 'dataItems'"):
            CL2ResultParser.parse('{"version": "v1"}')

    def test_empty_data_items(self):
        raw = json.dumps({"version": "v1", "dataItems": []})
        summary = CL2ResultParser.parse(raw)
        assert summary.pod_startup_latencies == []
        assert summary.api_latencies == []
        assert summary.scheduling_throughput is None

    def test_none_input_raises_parse_error(self):
        with pytest.raises(CL2ParseError, match="Invalid JSON"):
            CL2ResultParser.parse(None)  # type: ignore[arg-type]


class TestExtractPodStartupLatency:
    def test_extracts_p50_p90_p99(self):
        data = json.loads(SAMPLE_CL2_JSON)
        results = CL2ResultParser.extract_pod_startup_latency(data)
        assert len(results) == 3
        assert results[0].percentile == "P50"
        assert results[0].latency_ms == 1234.5
        assert results[1].percentile == "P90"
        assert results[1].latency_ms == 2345.6
        assert results[2].percentile == "P99"
        assert results[2].latency_ms == 5678.9

    def test_metric_name_is_pod_startup_latency(self):
        data = json.loads(SAMPLE_CL2_JSON)
        results = CL2ResultParser.extract_pod_startup_latency(data)
        for r in results:
            assert r.metric_name == "PodStartupLatency"

    def test_no_matching_items(self):
        data = {"dataItems": [{"labels": {"Metric": "other"}, "data": {}}]}
        assert CL2ResultParser.extract_pod_startup_latency(data) == []


class TestExtractAPIResponsiveness:
    def test_extracts_verb_resource_scope(self):
        data = json.loads(SAMPLE_CL2_JSON)
        results = CL2ResultParser.extract_api_responsiveness(data)
        assert len(results) == 1
        r = results[0]
        assert r.verb == "GET"
        assert r.resource == "pods"
        assert r.scope == "namespace"
        assert r.count == 1500

    def test_extracts_percentiles(self):
        data = json.loads(SAMPLE_CL2_JSON)
        r = CL2ResultParser.extract_api_responsiveness(data)[0]
        assert r.p50_ms == 10.2
        assert r.p90_ms == 25.4
        assert r.p99_ms == 100.1

    def test_no_matching_items(self):
        data = {"dataItems": [{"labels": {"Metric": "other"}, "data": {}}]}
        assert CL2ResultParser.extract_api_responsiveness(data) == []


class TestExtractSchedulingThroughput:
    def test_extracts_pods_per_second(self):
        data = json.loads(SAMPLE_CL2_JSON)
        result = CL2ResultParser.extract_scheduling_throughput(data)
        assert result is not None
        assert result.pods_per_second == 45.2

    def test_returns_none_when_absent(self):
        data = {"dataItems": [{"labels": {"Metric": "other"}, "data": {}}]}
        assert CL2ResultParser.extract_scheduling_throughput(data) is None
