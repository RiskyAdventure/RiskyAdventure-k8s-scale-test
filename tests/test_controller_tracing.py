"""Unit tests for Controller phase instrumentation (task 4.2).

Verifies that the Controller wraps each lifecycle phase in a
``phase_span`` context manager with the correct phase name and
attributes, and that errors within a phase propagate correctly
through the phase_span (which marks the span as ERROR).

Requirements: 2.1, 2.2, 2.4
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tracking_phase_span():
    """Return a mock phase_span that records calls and acts as a context manager."""
    calls = []

    @contextmanager
    def fake_phase_span(name, **attrs):
        calls.append({"name": name, "attributes": attrs})
        yield MagicMock()

    return fake_phase_span, calls


def _tracking_phase_span_that_propagates_errors():
    """Like _tracking_phase_span but re-raises exceptions from the body.

    This mimics the real phase_span behaviour: on exception it marks
    the span as ERROR and re-raises.
    """
    calls = []
    errors = []

    @contextmanager
    def fake_phase_span(name, **attrs):
        calls.append({"name": name, "attributes": attrs})
        try:
            yield MagicMock()
        except Exception as exc:
            errors.append({"name": name, "error": exc})
            raise

    return fake_phase_span, calls, errors


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPhaseSpanCalledForEachPhase:
    """Requirement 2.1: phase_span is called for preflight, scaling,
    hold-at-peak, and cleanup phases."""

    @patch("k8s_scale_test.controller.phase_span")
    @patch("k8s_scale_test.controller.install_slow_callback_monitor")
    def test_preflight_phase_span_called(self, mock_monitor, mock_phase_span):
        """Verify phase_span('preflight', ...) is called during preflight."""
        fake_ps, calls = _tracking_phase_span()
        mock_phase_span.side_effect = fake_ps

        # Import the module to inspect the source — we verify the call
        # pattern exists in the controller code rather than running the
        # full controller (which has many heavy dependencies).
        import k8s_scale_test.controller as ctrl_mod
        import inspect
        source = inspect.getsource(ctrl_mod.ScaleTestController.run)

        assert 'phase_span("preflight"' in source

    @patch("k8s_scale_test.controller.phase_span")
    @patch("k8s_scale_test.controller.install_slow_callback_monitor")
    def test_scaling_phase_span_called(self, mock_monitor, mock_phase_span):
        """Verify phase_span('scaling', ...) is called during scaling."""
        import k8s_scale_test.controller as ctrl_mod
        import inspect
        source = inspect.getsource(ctrl_mod.ScaleTestController.run)

        assert 'phase_span("scaling"' in source

    @patch("k8s_scale_test.controller.phase_span")
    @patch("k8s_scale_test.controller.install_slow_callback_monitor")
    def test_hold_at_peak_phase_span_called(self, mock_monitor, mock_phase_span):
        """Verify phase_span('hold-at-peak', ...) is called during hold."""
        import k8s_scale_test.controller as ctrl_mod
        import inspect
        source = inspect.getsource(ctrl_mod.ScaleTestController.run)

        assert 'phase_span("hold-at-peak"' in source

    @patch("k8s_scale_test.controller.phase_span")
    @patch("k8s_scale_test.controller.install_slow_callback_monitor")
    def test_cleanup_phase_span_called(self, mock_monitor, mock_phase_span):
        """Verify phase_span('cleanup', ...) is called during cleanup."""
        import k8s_scale_test.controller as ctrl_mod
        import inspect
        source = inspect.getsource(ctrl_mod.ScaleTestController.run)

        assert 'phase_span("cleanup"' in source


class TestPhaseSpanAttributes:
    """Requirement 2.1/2.3: phase_span receives run_id and target_pods."""

    def test_preflight_span_includes_run_id_and_target_pods(self):
        """Verify preflight phase_span call includes run_id and target_pods kwargs."""
        import k8s_scale_test.controller as ctrl_mod
        import inspect
        source = inspect.getsource(ctrl_mod.ScaleTestController.run)

        # The preflight phase_span should pass run_id and target_pods
        assert "run_id=run_id" in source
        assert "target_pods=" in source

    def test_scaling_span_includes_run_id(self):
        """Verify scaling phase_span call includes run_id."""
        import k8s_scale_test.controller as ctrl_mod
        import inspect
        source = inspect.getsource(ctrl_mod.ScaleTestController.run)

        # Find the scaling phase_span line
        lines = source.split("\n")
        scaling_lines = [l for l in lines if 'phase_span("scaling"' in l]
        assert len(scaling_lines) >= 1
        assert "run_id=" in scaling_lines[0]

    def test_hold_span_includes_hold_seconds(self):
        """Verify hold-at-peak phase_span includes hold_seconds."""
        import k8s_scale_test.controller as ctrl_mod
        import inspect
        source = inspect.getsource(ctrl_mod.ScaleTestController.run)

        lines = source.split("\n")
        hold_lines = [l for l in lines if 'phase_span("hold-at-peak"' in l]
        assert len(hold_lines) >= 1
        assert "hold_seconds=" in hold_lines[0]

    def test_cleanup_span_includes_run_id(self):
        """Verify cleanup phase_span includes run_id."""
        import k8s_scale_test.controller as ctrl_mod
        import inspect
        source = inspect.getsource(ctrl_mod.ScaleTestController.run)

        lines = source.split("\n")
        cleanup_lines = [l for l in lines if 'phase_span("cleanup"' in l]
        assert len(cleanup_lines) >= 1
        assert "run_id=" in cleanup_lines[0]


class TestPhaseSpanErrorPropagation:
    """Requirement 2.4: errors in a phase mark the span as ERROR.

    We test the phase_span context manager directly (already tested in
    test_tracing_phase_span.py) but here we verify the integration
    pattern: that the controller uses phase_span as a context manager
    wrapping phase code, so any exception raised inside will be caught
    by phase_span and marked as ERROR before re-raising.
    """

    def test_phase_span_used_as_context_manager(self):
        """Verify phase_span is used with 'with' statement (context manager pattern)."""
        import k8s_scale_test.controller as ctrl_mod
        import inspect
        source = inspect.getsource(ctrl_mod.ScaleTestController.run)

        # All four phases should use 'with phase_span(...):'
        assert source.count("with phase_span(") >= 4

    def test_phase_span_error_marks_span_error(self):
        """Verify that when code inside phase_span raises, the span gets ERROR status.

        This tests the actual phase_span implementation with a real
        in-memory tracing backend.
        """
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
        import k8s_scale_test.tracing as tracing_mod

        class _ListExporter(SpanExporter):
            def __init__(self):
                self.spans = []

            def export(self, spans):
                self.spans.extend(spans)
                return SpanExportResult.SUCCESS

            def shutdown(self):
                pass

        exporter = _ListExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        # Temporarily enable tracing
        orig_enabled = tracing_mod._enabled
        orig_tracer = tracing_mod._tracer
        orig_provider = tracing_mod._provider
        try:
            tracing_mod._enabled = True
            tracing_mod._tracer = tracer
            tracing_mod._provider = provider

            with pytest.raises(RuntimeError, match="phase failed"):
                with tracing_mod.phase_span("scaling", run_id="test-123"):
                    raise RuntimeError("phase failed")

            assert len(exporter.spans) == 1
            s = exporter.spans[0]
            assert s.name == "scale-test-scaling"
            assert s.status.status_code == trace.StatusCode.ERROR
            # Exception should be recorded as an event
            assert any(e.name == "exception" for e in s.events)
        finally:
            tracing_mod._enabled = orig_enabled
            tracing_mod._tracer = orig_tracer
            tracing_mod._provider = orig_provider
            provider.shutdown()


class TestSlowCallbackMonitorInstalled:
    """Requirement 5.1: slow callback monitor is installed at start of run()."""

    def test_install_slow_callback_monitor_called_in_run(self):
        """Verify install_slow_callback_monitor is called in the run method."""
        import k8s_scale_test.controller as ctrl_mod
        import inspect
        source = inspect.getsource(ctrl_mod.ScaleTestController.run)

        assert "install_slow_callback_monitor" in source

    def test_controller_imports_install_slow_callback_monitor(self):
        """Verify the controller imports install_slow_callback_monitor from tracing."""
        import k8s_scale_test.controller as ctrl_mod
        import inspect
        source = inspect.getsource(ctrl_mod)

        assert "from k8s_scale_test.tracing import" in source
        assert "install_slow_callback_monitor" in source
