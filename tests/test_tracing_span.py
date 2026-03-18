"""Unit tests for tracing.span context manager (task 1.3).

Covers: naming, attributes, error handling, no-op when disabled.
Requirements: 3.3, 4.2, 4.3, 4.4, 6.1
"""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

import k8s_scale_test.tracing as tracing_mod


class _ListExporter(SpanExporter):
    """Minimal exporter that collects finished spans in a list."""

    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=None):
        return True


@pytest.fixture(autouse=True)
def _setup_tracing(monkeypatch):
    """Set up an in-memory tracing environment for each test."""
    exporter = _ListExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    tracer = provider.get_tracer("test")

    monkeypatch.setattr(tracing_mod, "_enabled", True)
    monkeypatch.setattr(tracing_mod, "_tracer", tracer)
    monkeypatch.setattr(tracing_mod, "_provider", provider)

    yield exporter

    provider.shutdown()


class TestSpanNaming:
    """Requirement 4.2, 6.1: span uses the name directly."""

    def test_span_name_simple(self, _setup_tracing):
        exporter = _setup_tracing
        with tracing_mod.span("monitor/tick"):
            pass
        assert len(exporter.spans) == 1
        assert exporter.spans[0].name == "monitor/tick"

    def test_span_name_with_prefix(self, _setup_tracing):
        exporter = _setup_tracing
        with tracing_mod.span("k8s/list_pods"):
            pass
        assert exporter.spans[0].name == "k8s/list_pods"

    def test_span_name_alert(self, _setup_tracing):
        exporter = _setup_tracing
        with tracing_mod.span("alert/stuck_pods"):
            pass
        assert exporter.spans[0].name == "alert/stuck_pods"


class TestSpanAttributes:
    """Requirement 4.3, 4.4: attributes stored on span."""

    def test_attributes_applied(self, _setup_tracing):
        exporter = _setup_tracing
        with tracing_mod.span("promql/query", query="up", source="amp"):
            pass
        attrs = dict(exporter.spans[0].attributes)
        assert attrs["query"] == "up"
        assert attrs["source"] == "amp"

    def test_no_attributes(self, _setup_tracing):
        exporter = _setup_tracing
        with tracing_mod.span("investigation/k8s_events"):
            pass
        assert len(exporter.spans[0].attributes) == 0

    def test_numeric_attribute(self, _setup_tracing):
        exporter = _setup_tracing
        with tracing_mod.span("monitor/tick", tick_number=42):
            pass
        assert dict(exporter.spans[0].attributes)["tick_number"] == 42


class TestSpanErrorHandling:
    """Requirement 3.3: error sets status to ERROR and records exception."""

    def test_exception_sets_error_status(self, _setup_tracing):
        exporter = _setup_tracing
        with pytest.raises(ValueError, match="boom"):
            with tracing_mod.span("k8s/list_pods"):
                raise ValueError("boom")
        s = exporter.spans[0]
        assert s.status.status_code == trace.StatusCode.ERROR

    def test_exception_is_recorded(self, _setup_tracing):
        exporter = _setup_tracing
        with pytest.raises(RuntimeError):
            with tracing_mod.span("promql/query"):
                raise RuntimeError("timeout")
        events = exporter.spans[0].events
        assert any(e.name == "exception" for e in events)

    def test_exception_is_reraised(self, _setup_tracing):
        with pytest.raises(TypeError, match="bad arg"):
            with tracing_mod.span("investigation/eni"):
                raise TypeError("bad arg")


class TestSpanYieldsSpan:
    """span yields the span object when enabled."""

    def test_yields_span_object(self, _setup_tracing):
        with tracing_mod.span("monitor/tick") as s:
            assert s is not None
            assert hasattr(s, "set_attribute")


class TestSpanDisabled:
    """No-op when tracing is disabled."""

    def test_noop_when_disabled(self, monkeypatch):
        monkeypatch.setattr(tracing_mod, "_enabled", False)
        with tracing_mod.span("k8s/list_pods", ns="default") as s:
            assert s is None

    def test_noop_when_tracer_is_none(self, monkeypatch):
        monkeypatch.setattr(tracing_mod, "_enabled", True)
        monkeypatch.setattr(tracing_mod, "_tracer", None)
        with tracing_mod.span("promql/query") as s:
            assert s is None

    def test_noop_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(tracing_mod, "_enabled", False)
        # Should not raise even with attributes
        with tracing_mod.span("alert/test", severity="high") as s:
            assert s is None


class TestSpanChildRelationship:
    """Verify span creates a child under the current active span."""

    def test_child_shares_trace_id_with_parent(self, _setup_tracing):
        exporter = _setup_tracing
        with tracing_mod.phase_span("scaling") as parent:
            with tracing_mod.span("monitor/tick") as child:
                pass
        assert len(exporter.spans) == 2
        child_span = exporter.spans[0]  # child finishes first
        parent_span = exporter.spans[1]
        assert child_span.context.trace_id == parent_span.context.trace_id
