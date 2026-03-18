"""Unit tests for tracing.trace_thread decorator (task 1.4).

Covers: span naming (thread/{name}), no-op when disabled, exception handling.
Requirements: 3.1, 3.4, 8.2, 8.4
"""

from __future__ import annotations

import threading

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


class TestTraceThreadNaming:
    """Requirement 3.1: span named thread/{thread_name}."""

    def test_span_name_simple(self, _setup_tracing):
        exporter = _setup_tracing

        @tracing_mod.trace_thread("watch/deployments")
        def worker():
            pass

        worker()
        assert len(exporter.spans) == 1
        assert exporter.spans[0].name == "thread/watch/deployments"

    def test_span_name_ticker(self, _setup_tracing):
        exporter = _setup_tracing

        @tracing_mod.trace_thread("ticker")
        def ticker():
            pass

        ticker()
        assert exporter.spans[0].name == "thread/ticker"

    def test_span_name_watch_nodes(self, _setup_tracing):
        exporter = _setup_tracing

        @tracing_mod.trace_thread("watch/nodes")
        def watch():
            pass

        watch()
        assert exporter.spans[0].name == "thread/watch/nodes"


class TestTraceThreadReturnValue:
    """Decorated function return value is preserved."""

    def test_returns_value(self, _setup_tracing):
        @tracing_mod.trace_thread("worker")
        def compute():
            return 42

        assert compute() == 42

    def test_returns_none(self, _setup_tracing):
        @tracing_mod.trace_thread("worker")
        def noop():
            pass

        assert noop() is None


class TestTraceThreadErrorHandling:
    """Exception sets span status to ERROR and re-raises."""

    def test_exception_sets_error_status(self, _setup_tracing):
        exporter = _setup_tracing

        @tracing_mod.trace_thread("failing")
        def boom():
            raise ValueError("kaboom")

        with pytest.raises(ValueError, match="kaboom"):
            boom()

        s = exporter.spans[0]
        assert s.status.status_code == trace.StatusCode.ERROR

    def test_exception_is_recorded(self, _setup_tracing):
        exporter = _setup_tracing

        @tracing_mod.trace_thread("failing")
        def boom():
            raise RuntimeError("timeout")

        with pytest.raises(RuntimeError):
            boom()

        events = exporter.spans[0].events
        assert any(e.name == "exception" for e in events)

    def test_exception_is_reraised(self, _setup_tracing):
        @tracing_mod.trace_thread("failing")
        def boom():
            raise TypeError("bad arg")

        with pytest.raises(TypeError, match="bad arg"):
            boom()


class TestTraceThreadDisabled:
    """No-op when tracing is disabled."""

    def test_noop_when_disabled(self, monkeypatch):
        monkeypatch.setattr(tracing_mod, "_enabled", False)

        @tracing_mod.trace_thread("worker")
        def fn():
            return "ok"

        assert fn() == "ok"

    def test_noop_when_tracer_is_none(self, monkeypatch):
        monkeypatch.setattr(tracing_mod, "_enabled", True)
        monkeypatch.setattr(tracing_mod, "_tracer", None)

        @tracing_mod.trace_thread("worker")
        def fn():
            return "ok"

        assert fn() == "ok"

    def test_noop_no_spans_created(self, monkeypatch, _setup_tracing):
        exporter = _setup_tracing
        monkeypatch.setattr(tracing_mod, "_enabled", False)

        @tracing_mod.trace_thread("worker")
        def fn():
            pass

        fn()
        assert len(exporter.spans) == 0


class TestTraceThreadPreservesMetadata:
    """functools.wraps preserves the original function metadata."""

    def test_preserves_name(self):
        @tracing_mod.trace_thread("worker")
        def my_function():
            """My docstring."""
            pass

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."


class TestTraceThreadWithArguments:
    """Decorated function receives args and kwargs correctly."""

    def test_passes_args(self, _setup_tracing):
        @tracing_mod.trace_thread("worker")
        def add(a, b):
            return a + b

        assert add(3, 4) == 7

    def test_passes_kwargs(self, _setup_tracing):
        @tracing_mod.trace_thread("worker")
        def greet(name="world"):
            return f"hello {name}"

        assert greet(name="test") == "hello test"


class TestTraceThreadInRealThread:
    """Verify the decorator works when called from an actual OS thread."""

    def test_span_created_in_thread(self, _setup_tracing):
        exporter = _setup_tracing
        result = []

        @tracing_mod.trace_thread("bg")
        def worker():
            result.append("done")

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)

        assert result == ["done"]
        assert len(exporter.spans) == 1
        assert exporter.spans[0].name == "thread/bg"
