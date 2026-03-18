"""Unit tests for Scanner and Health Sweep tracing instrumentation (task 7.3).

Verifies that the ObservabilityScanner and Health Sweep components use
the correct span calls for PromQL queries, CloudWatch Insights queries,
K8s API operations, and AMP metric collection.

Uses ``inspect.getsource()`` to verify span calls are present in the
source code — the same approach as the controller, monitor, and anomaly tests.

Requirements: 4.2, 4.3, 4.4, 8.2
"""

from __future__ import annotations

import inspect

import k8s_scale_test.observability as obs_mod
import k8s_scale_test.health_sweep as hs_mod
from k8s_scale_test.observability import ObservabilityScanner
from k8s_scale_test.health_sweep import (
    AMPMetricCollector,
    K8sConditionChecker,
)


# ---------------------------------------------------------------------------
# Requirement 4.3 — PromQL query spans in ObservabilityScanner
# ---------------------------------------------------------------------------

class TestScannerPromQLSpans:
    """Verify ObservabilityScanner wraps PromQL queries in span('promql/query')."""

    def test_run_query_creates_promql_span(self):
        """_run_query should use span('promql/query', ...) for Prometheus queries."""
        source = inspect.getsource(ObservabilityScanner._run_query)
        assert 'span("promql/query"' in source

    def test_promql_span_includes_query_attribute(self):
        """The promql/query span should include the query string as an attribute."""
        source = inspect.getsource(ObservabilityScanner._run_query)
        assert "query=query.query" in source


# ---------------------------------------------------------------------------
# Requirement 4.4 — CloudWatch Insights query spans in ObservabilityScanner
# ---------------------------------------------------------------------------

class TestScannerCloudWatchSpans:
    """Verify ObservabilityScanner wraps CW Insights queries in span('cloudwatch/insights_query')."""

    def test_run_query_creates_cloudwatch_span(self):
        """_run_query should use span('cloudwatch/insights_query', ...) for CW queries."""
        source = inspect.getsource(ObservabilityScanner._run_query)
        assert 'span("cloudwatch/insights_query"' in source

    def test_cloudwatch_span_includes_query_attribute(self):
        """The cloudwatch/insights_query span should include the query string."""
        source = inspect.getsource(ObservabilityScanner._run_query)
        assert "query=query.query" in source


# ---------------------------------------------------------------------------
# Requirement 4.3 — AMP PromQL manual spans in health_sweep.py
# ---------------------------------------------------------------------------

class TestAMPCollectorPromQLSpans:
    """Verify AMPMetricCollector._query_promql creates manual promql/query spans.

    AMP PromQL queries use raw urllib.request with SigV4 signing, NOT boto3,
    so they are NOT auto-captured by the botocore instrumentor and require
    explicit span() wrapping.
    """

    def test_query_promql_creates_promql_span(self):
        """_query_promql should use span('promql/query', ...) for AMP queries."""
        source = inspect.getsource(AMPMetricCollector._query_promql)
        assert 'span("promql/query"' in source

    def test_query_promql_span_includes_source_attribute(self):
        """The promql/query span in _query_promql should include source='amp_collector'."""
        source = inspect.getsource(AMPMetricCollector._query_promql)
        assert 'source="amp_collector"' in source

    def test_query_promql_span_includes_query_attribute(self):
        """The promql/query span should include the query string as an attribute."""
        source = inspect.getsource(AMPMetricCollector._query_promql)
        assert "query=query" in source


# ---------------------------------------------------------------------------
# Requirement 4.2 — K8s operation spans in health_sweep.py
# ---------------------------------------------------------------------------

class TestK8sConditionCheckerSpans:
    """Verify K8sConditionChecker wraps K8s API calls in span('k8s/list_nodes')."""

    def test_check_all_creates_k8s_list_nodes_span(self):
        """check_all should use span('k8s/list_nodes') for the node listing call."""
        source = inspect.getsource(K8sConditionChecker.check_all)
        assert 'span("k8s/list_nodes")' in source


# ---------------------------------------------------------------------------
# Requirement 8.2 — Module-level imports verify tracing integration
# ---------------------------------------------------------------------------

class TestObservabilityTracingImports:
    """Verify observability.py imports span from the tracing module."""

    def test_imports_span(self):
        """observability.py should import span from k8s_scale_test.tracing."""
        source = inspect.getsource(obs_mod)
        assert "from k8s_scale_test.tracing import" in source
        assert "span" in source


class TestHealthSweepTracingImports:
    """Verify health_sweep.py imports span from the tracing module."""

    def test_imports_span(self):
        """health_sweep.py should import span from k8s_scale_test.tracing."""
        source = inspect.getsource(hs_mod)
        assert "from k8s_scale_test.tracing import" in source
        assert "span" in source
