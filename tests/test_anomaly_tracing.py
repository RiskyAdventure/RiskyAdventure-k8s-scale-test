"""Unit tests for Anomaly Detector tracing instrumentation (task 6.2).

Verifies that the AnomalyDetector uses the correct span calls for the
alert handler, each investigation layer, and sets root_cause/severity
attributes on the alert span.

Uses ``inspect.getsource()`` to verify span calls are present in the
source code — the same approach as the controller and monitor tests.

Requirements: 6.1, 6.2, 6.3
"""

from __future__ import annotations

import inspect

import k8s_scale_test.anomaly as anomaly_mod
from k8s_scale_test.anomaly import AnomalyDetector


# ---------------------------------------------------------------------------
# Requirement 6.1 — Alert span wraps handle_alert
# ---------------------------------------------------------------------------

class TestAlertSpan:
    """Verify handle_alert wraps the investigation in an alert span."""

    def test_handle_alert_creates_alert_span(self):
        """handle_alert should use span('alert/...') for the alert type."""
        source = inspect.getsource(AnomalyDetector.handle_alert)
        assert 'span("alert/"' in source

    def test_alert_span_includes_alert_id_attribute(self):
        """The alert span should include alert_id as an attribute."""
        source = inspect.getsource(AnomalyDetector.handle_alert)
        assert "alert_id=" in source


# ---------------------------------------------------------------------------
# Requirement 6.2 — Investigation layer spans
# ---------------------------------------------------------------------------

class TestInvestigationLayerSpans:
    """Verify each investigation layer is wrapped in a span."""

    def test_k8s_events_span(self):
        """K8s events layer should be wrapped in span('investigation/k8s_events')."""
        source = inspect.getsource(AnomalyDetector.handle_alert)
        assert 'span("investigation/k8s_events")' in source

    def test_pod_phases_span(self):
        """Pod phases layer should be wrapped in span('investigation/pod_phases')."""
        source = inspect.getsource(AnomalyDetector.handle_alert)
        assert 'span("investigation/pod_phases")' in source

    def test_stuck_nodes_span(self):
        """Stuck nodes layer should be wrapped in span('investigation/stuck_nodes')."""
        source = inspect.getsource(AnomalyDetector.handle_alert)
        assert 'span("investigation/stuck_nodes")' in source

    def test_eni_span(self):
        """ENI state layer should be wrapped in span('investigation/eni')."""
        source = inspect.getsource(AnomalyDetector.handle_alert)
        assert 'span("investigation/eni")' in source

    def test_ssm_span(self):
        """SSM diagnostics layer should be wrapped in span('investigation/ssm')."""
        source = inspect.getsource(AnomalyDetector.handle_alert)
        assert 'span("investigation/ssm")' in source


# ---------------------------------------------------------------------------
# Requirement 6.3 — Root cause and severity attributes on alert span
# ---------------------------------------------------------------------------

class TestAlertSpanAttributes:
    """Verify root_cause and severity are set on the alert span."""

    def test_root_cause_attribute_set(self):
        """The alert span should have root_cause set via set_attribute."""
        source = inspect.getsource(AnomalyDetector.handle_alert)
        assert 'set_attribute("root_cause"' in source

    def test_severity_attribute_set(self):
        """The alert span should have severity set via set_attribute."""
        source = inspect.getsource(AnomalyDetector.handle_alert)
        assert 'set_attribute("severity"' in source


# ---------------------------------------------------------------------------
# Module-level imports
# ---------------------------------------------------------------------------

class TestAnomalyTracingImports:
    """Verify anomaly.py imports the required tracing helpers."""

    def test_imports_span(self):
        """anomaly.py should import span from the tracing module."""
        source = inspect.getsource(anomaly_mod)
        assert "from k8s_scale_test.tracing import" in source
        assert "span" in source
