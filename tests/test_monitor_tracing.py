"""Unit tests for Monitor tracing instrumentation (task 5.2).

Verifies that the PodRateMonitor uses the correct tracing decorators
and span calls for watch threads, the ticker loop, and the
``_safe_callback`` investigation dispatch.

Uses ``inspect.getsource()`` to verify decorators and span calls are
present in the source code — the same approach as the controller tests.

Requirements: 3.1, 3.3, 3.4
"""

from __future__ import annotations

import inspect

import k8s_scale_test.monitor as monitor_mod
from k8s_scale_test.monitor import PodRateMonitor


# ---------------------------------------------------------------------------
# Requirement 3.1 — Watch thread spans have correct names
# ---------------------------------------------------------------------------

class TestWatchThreadDecorators:
    """Verify watch thread methods are decorated with @trace_thread."""

    def test_watch_deployments_has_trace_thread_decorator(self):
        """_watch_deployments should be decorated with @trace_thread('watch/deployments')."""
        source = inspect.getsource(PodRateMonitor._watch_deployments)
        assert '@trace_thread("watch/deployments")' in source

    def test_watch_nodes_has_trace_thread_decorator(self):
        """_watch_nodes should be decorated with @trace_thread('watch/nodes')."""
        source = inspect.getsource(PodRateMonitor._watch_nodes)
        assert '@trace_thread("watch/nodes")' in source


# ---------------------------------------------------------------------------
# Requirement 3.3 — Ticker thread spans
# ---------------------------------------------------------------------------

class TestTickerThreadTracing:
    """Verify the ticker thread is traced and creates per-tick spans."""

    def test_ticker_thread_has_trace_thread_decorator(self):
        """_ticker_thread should be decorated with @trace_thread('ticker')."""
        source = inspect.getsource(PodRateMonitor._ticker_thread)
        assert '@trace_thread("ticker")' in source

    def test_ticker_thread_creates_per_tick_span(self):
        """Each tick iteration should be wrapped in span('monitor/tick')."""
        source = inspect.getsource(PodRateMonitor._ticker_thread)
        assert 'span("monitor/tick")' in source


# ---------------------------------------------------------------------------
# Requirement 3.4 — _safe_callback investigation span
# ---------------------------------------------------------------------------

class TestSafeCallbackTracing:
    """Verify _safe_callback creates an anomaly/investigation span."""

    def test_safe_callback_creates_investigation_span(self):
        """_safe_callback's _run_in_thread should use span('anomaly/investigation', ...)."""
        source = inspect.getsource(PodRateMonitor._safe_callback)
        assert 'span("anomaly/investigation"' in source

    def test_safe_callback_passes_alert_type_attribute(self):
        """The investigation span should include alert_type as an attribute."""
        source = inspect.getsource(PodRateMonitor._safe_callback)
        assert "alert_type=" in source


# ---------------------------------------------------------------------------
# Module-level imports
# ---------------------------------------------------------------------------

class TestMonitorTracingImports:
    """Verify monitor.py imports the required tracing helpers."""

    def test_imports_trace_thread(self):
        """monitor.py should import trace_thread from tracing module."""
        source = inspect.getsource(monitor_mod)
        assert "trace_thread" in source

    def test_imports_span(self):
        """monitor.py should import span from tracing module."""
        source = inspect.getsource(monitor_mod)
        assert "from k8s_scale_test.tracing import" in source
        assert "span" in source
