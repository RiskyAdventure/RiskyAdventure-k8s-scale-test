"""Unit tests for tracing.phase_span context manager (task 1.2).

Uses a simple list-based span exporter to capture spans without real API calls.
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
    """Set up an in-memory tracing environment for each test.

    We avoid calling ``trace.set_tracer_provider()`` more than once
    (the SDK rejects overrides).  Instead we create a fresh
    TracerProvider per test and inject it directly into the tracing
    module's globals.
    """
    exporter = _ListExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    tracer = provider.get_tracer("test")

    monkeypatch.setattr(tracing_mod, "_enabled", True)
    monkeypatch.setattr(tracing_mod, "_tracer", tracer)
    monkeypatch.setattr(tracing_mod, "_provider", provider)

    yield exporter

    provider.shutdown()


class TestPhaseSpanNaming:
    """Requirement 2.1: span named scale-test-{phase}."""

    def test_span_name_matches_phase(self, _setup_tracing):
        exporter = _setup_tracing
        with tracing_mod.phase_span("scaling"):
            pass
        spans = exporter.spans
        assert len(spans) == 1
        assert spans[0].name == "scale-test-scaling"

    def test_span_name_preflight(self, _setup_tracing):
        exporter = _setup_tracing
        with tracing_mod.phase_span("preflight"):
            pass
        assert exporter.spans[0].name == "scale-test-preflight"


class TestPhaseSpanDuration:
    """Requirement 2.2: span closes with duration recorded."""

    def test_span_has_nonnegative_duration(self, _setup_tracing):
        exporter = _setup_tracing
        with tracing_mod.phase_span("hold-at-peak"):
            pass
        s = exporter.spans[0]
        assert s.end_time is not None
        assert s.start_time is not None
        assert s.end_time >= s.start_time


class TestPhaseSpanAttributes:
    """Requirement 2.3: attributes set on span."""

    def test_attributes_applied(self, _setup_tracing):
        exporter = _setup_tracing
        with tracing_mod.phase_span("scaling", run_id="abc-123", target_pods=500):
            pass
        attrs = dict(exporter.spans[0].attributes)
        assert attrs["run_id"] == "abc-123"
        assert attrs["target_pods"] == 500

    def test_no_attributes(self, _setup_tracing):
        exporter = _setup_tracing
        with tracing_mod.phase_span("cleanup"):
            pass
        assert len(exporter.spans[0].attributes) == 0


class TestPhaseSpanErrorHandling:
    """Requirement 2.4: error marks span as ERROR and records exception."""

    def test_exception_sets_error_status(self, _setup_tracing):
        exporter = _setup_tracing
        with pytest.raises(ValueError, match="boom"):
            with tracing_mod.phase_span("scaling"):
                raise ValueError("boom")
        s = exporter.spans[0]
        assert s.status.status_code == trace.StatusCode.ERROR

    def test_exception_is_recorded(self, _setup_tracing):
        exporter = _setup_tracing
        with pytest.raises(RuntimeError):
            with tracing_mod.phase_span("preflight"):
                raise RuntimeError("network timeout")
        events = exporter.spans[0].events
        assert any(e.name == "exception" for e in events)

    def test_exception_is_reraised(self, _setup_tracing):
        with pytest.raises(TypeError, match="bad type"):
            with tracing_mod.phase_span("cleanup"):
                raise TypeError("bad type")


class TestPhaseSpanYieldsSpan:
    """phase_span yields the span object when enabled."""

    def test_yields_span_object(self, _setup_tracing):
        with tracing_mod.phase_span("scaling") as s:
            assert s is not None
            assert hasattr(s, "set_attribute")


class TestPhaseSpanDisabled:
    """No-op when tracing is disabled."""

    def test_noop_when_disabled(self, monkeypatch):
        monkeypatch.setattr(tracing_mod, "_enabled", False)
        with tracing_mod.phase_span("scaling", run_id="x") as s:
            assert s is None

    def test_noop_when_tracer_is_none(self, monkeypatch):
        monkeypatch.setattr(tracing_mod, "_enabled", True)
        monkeypatch.setattr(tracing_mod, "_tracer", None)
        with tracing_mod.phase_span("scaling") as s:
            assert s is None
