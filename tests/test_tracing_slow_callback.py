"""Unit tests for tracing.install_slow_callback_monitor (task 1.5).

Uses a real asyncio event loop with an in-memory span exporter to verify
that the slow-callback monitor detects event loop blockages and records
them as ``asyncio/slow_callback`` spans.
"""

from __future__ import annotations

import asyncio
import time

import pytest
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


@pytest.fixture()
def tracing_env(monkeypatch):
    """Set up an in-memory tracing environment for each test."""
    exporter = _ListExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    monkeypatch.setattr(tracing_mod, "_enabled", True)
    monkeypatch.setattr(tracing_mod, "_tracer", tracer)
    monkeypatch.setattr(tracing_mod, "_provider", provider)

    yield exporter

    monkeypatch.setattr(tracing_mod, "_enabled", False)
    provider.shutdown()


class TestSlowCallbackMonitorDetection:
    """Requirement 5.1: detect callbacks exceeding threshold."""

    def test_detects_blocked_loop(self, tracing_env):
        """A synchronous sleep longer than threshold produces a span."""
        exporter = tracing_env

        async def _run():
            loop = asyncio.get_running_loop()
            tracing_mod.install_slow_callback_monitor(loop, threshold_ms=50)
            # Let the first probe schedule
            await asyncio.sleep(0.06)
            # Block the loop well beyond the 50 ms threshold
            time.sleep(0.15)
            # Give the probe a chance to fire and detect the blockage
            await asyncio.sleep(0.1)

        asyncio.run(_run())

        slow_spans = [s for s in exporter.spans if s.name == "asyncio/slow_callback"]
        assert len(slow_spans) >= 1, "Expected at least one slow_callback span"

    def test_no_span_when_loop_is_responsive(self, tracing_env):
        """No slow_callback span when the loop is never blocked."""
        exporter = tracing_env

        async def _run():
            loop = asyncio.get_running_loop()
            # Use a high threshold so normal scheduling jitter doesn't trigger
            tracing_mod.install_slow_callback_monitor(loop, threshold_ms=500)
            # Just let the loop run normally for a bit
            await asyncio.sleep(0.2)

        asyncio.run(_run())

        slow_spans = [s for s in exporter.spans if s.name == "asyncio/slow_callback"]
        assert len(slow_spans) == 0, "No slow_callback spans expected"


class TestSlowCallbackSpanAttributes:
    """Requirement 5.2: span has delay_ms attribute."""

    def test_span_has_delay_ms_attribute(self, tracing_env):
        exporter = tracing_env

        async def _run():
            loop = asyncio.get_running_loop()
            tracing_mod.install_slow_callback_monitor(loop, threshold_ms=50)
            await asyncio.sleep(0.06)
            time.sleep(0.15)
            await asyncio.sleep(0.1)

        asyncio.run(_run())

        slow_spans = [s for s in exporter.spans if s.name == "asyncio/slow_callback"]
        assert len(slow_spans) >= 1
        attrs = dict(slow_spans[0].attributes)
        assert "delay_ms" in attrs
        assert attrs["delay_ms"] > 50  # Must exceed the threshold

    def test_delay_ms_reflects_actual_blockage(self, tracing_env):
        """delay_ms should be roughly proportional to the blockage time."""
        exporter = tracing_env

        async def _run():
            loop = asyncio.get_running_loop()
            tracing_mod.install_slow_callback_monitor(loop, threshold_ms=50)
            await asyncio.sleep(0.06)
            # Block for ~200ms
            time.sleep(0.2)
            await asyncio.sleep(0.1)

        asyncio.run(_run())

        slow_spans = [s for s in exporter.spans if s.name == "asyncio/slow_callback"]
        assert len(slow_spans) >= 1
        delay = slow_spans[0].attributes["delay_ms"]
        # The delay should be at least 100ms (200ms block minus 50ms probe interval, roughly)
        assert delay > 100


class TestSlowCallbackMonitorNoop:
    """No-op when tracing is disabled."""

    def test_noop_when_disabled(self, monkeypatch):
        """install_slow_callback_monitor does nothing when _enabled is False."""
        monkeypatch.setattr(tracing_mod, "_enabled", False)

        calls = []

        class _FakeLoop:
            def call_later(self, delay, cb, *args):
                calls.append((delay, cb, args))

        tracing_mod.install_slow_callback_monitor(_FakeLoop(), threshold_ms=100)
        assert len(calls) == 0, "Should not schedule anything when disabled"


class TestSlowCallbackMonitorReschedules:
    """The probe reschedules itself to keep monitoring."""

    def test_probe_fires_multiple_times(self, tracing_env):
        exporter = tracing_env

        async def _run():
            loop = asyncio.get_running_loop()
            tracing_mod.install_slow_callback_monitor(loop, threshold_ms=30)
            # Block twice with small gaps
            await asyncio.sleep(0.06)
            time.sleep(0.1)
            await asyncio.sleep(0.1)
            time.sleep(0.1)
            await asyncio.sleep(0.1)

        asyncio.run(_run())

        slow_spans = [s for s in exporter.spans if s.name == "asyncio/slow_callback"]
        assert len(slow_spans) >= 2, "Probe should detect multiple blockages"
