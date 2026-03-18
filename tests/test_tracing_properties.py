"""Property-based tests for the tracing module.

Uses Hypothesis to verify correctness properties of the tracing module
across randomly generated inputs. Each test uses a _ListExporter to
capture spans without making real API calls.

Feature: xray-tracing
"""

from __future__ import annotations

import contextlib

from hypothesis import given, settings
from hypothesis import strategies as st
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

import k8s_scale_test.tracing as tracing_mod


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

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


@contextlib.contextmanager
def _tracing_env():
    """Context manager that sets up in-memory tracing and yields the exporter."""
    exporter = _ListExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    old_enabled = tracing_mod._enabled
    old_tracer = tracing_mod._tracer
    old_provider = tracing_mod._provider

    tracing_mod._enabled = True
    tracing_mod._tracer = tracer
    tracing_mod._provider = provider

    try:
        yield exporter
    finally:
        provider.shutdown()
        tracing_mod._enabled = old_enabled
        tracing_mod._tracer = old_tracer
        tracing_mod._provider = old_provider


# Reusable strategies
_safe_text = st.text(
    min_size=1, max_size=50,
    alphabet=st.characters(whitelist_categories=("L", "N")),
)

_attr_keys = st.text(
    min_size=1, max_size=20,
    alphabet=st.characters(whitelist_categories=("L",)),
).filter(lambda k: k not in ("name", "phase_name"))

_attr_values = st.one_of(
    st.text(min_size=1, max_size=50),
    st.integers(min_value=-1000, max_value=1000),
)

_attr_dicts = st.dictionaries(keys=_attr_keys, values=_attr_values)


# ---------------------------------------------------------------------------
# Feature: xray-tracing, Property 1: Phase span lifecycle
# ---------------------------------------------------------------------------

class TestProperty1PhaseSpanLifecycle:
    """Property 1: Phase span lifecycle.

    For any phase name string, calling phase_span(name) as a context manager
    and exiting normally SHALL produce a closed span whose name equals
    scale-test-{name} and whose recorded duration is non-negative.

    **Validates: Requirements 2.1, 2.2**
    """

    @given(phase_name=_safe_text)
    @settings(max_examples=100)
    def test_phase_span_name_and_duration(self, phase_name):
        with _tracing_env() as exporter:
            with tracing_mod.phase_span(phase_name):
                pass

            assert len(exporter.spans) == 1
            s = exporter.spans[0]
            assert s.name == f"scale-test-{phase_name}"
            duration_ns = s.end_time - s.start_time
            assert duration_ns >= 0


# ---------------------------------------------------------------------------
# Feature: xray-tracing, Property 2: Span attributes
# ---------------------------------------------------------------------------

class TestProperty2SpanAttributes:
    """Property 2: Span attributes.

    For any dictionary of attribute key-value pairs passed to phase_span(),
    all key-value pairs SHALL be present as attributes on the resulting span.

    **Validates: Requirements 2.3, 6.3**
    """

    @given(attrs=_attr_dicts)
    @settings(max_examples=100)
    def test_phase_span_attributes(self, attrs):
        with _tracing_env() as exporter:
            with tracing_mod.phase_span("test_phase", **attrs):
                pass

            assert len(exporter.spans) == 1
            recorded = dict(exporter.spans[0].attributes)
            for key, value in attrs.items():
                assert key in recorded
                assert recorded[key] == value


# ---------------------------------------------------------------------------
# Feature: xray-tracing, Property 3: Child span naming convention
# ---------------------------------------------------------------------------

class TestProperty3ChildSpanNaming:
    """Property 3: Child span naming convention.

    For any prefix string and name string, span("{prefix}/{name}") SHALL
    create a span whose name equals "{prefix}/{name}".

    **Validates: Requirements 3.1, 4.2, 6.1**
    """

    @given(prefix=_safe_text, name=_safe_text)
    @settings(max_examples=100)
    def test_child_span_naming(self, prefix, name):
        with _tracing_env() as exporter:
            expected_name = f"{prefix}/{name}"
            with tracing_mod.span(expected_name):
                pass

            assert len(exporter.spans) == 1
            assert exporter.spans[0].name == expected_name


# ---------------------------------------------------------------------------
# Feature: xray-tracing, Property 4: Span attribute recording
# ---------------------------------------------------------------------------

class TestProperty4SpanAttributeRecording:
    """Property 4: Span attribute recording.

    For any set of keyword attribute arguments passed to span(name, **attrs),
    all key-value pairs SHALL appear in the span's attributes.

    **Validates: Requirements 4.3, 4.4**
    """

    @given(attrs=_attr_dicts)
    @settings(max_examples=100)
    def test_span_attribute_recording(self, attrs):
        with _tracing_env() as exporter:
            with tracing_mod.span("test_op", **attrs):
                pass

            assert len(exporter.spans) == 1
            recorded = dict(exporter.spans[0].attributes)
            for key, value in attrs.items():
                assert key in recorded
                assert recorded[key] == value


# ---------------------------------------------------------------------------
# Feature: xray-tracing, Property 5: Trace URL format
# ---------------------------------------------------------------------------

class TestProperty5TraceUrlFormat:
    """Property 5: Trace URL format.

    For any run_id string, get_trace_url(run_id) SHALL return a URL matching
    the expected X-Ray console deep-link pattern.

    **Validates: Requirements 7.4**
    """

    @given(run_id=_safe_text)
    @settings(max_examples=100)
    def test_trace_url_format(self, run_id):
        import urllib.parse

        region = "us-west-2"
        old_region = tracing_mod._region
        tracing_mod._region = region
        try:
            url = tracing_mod.get_trace_url(run_id)

            encoded_run_id = urllib.parse.quote(run_id, safe="")
            expected = (
                f"https://{region}.console.aws.amazon.com/cloudwatch/home"
                f"?region={region}#xray:traces"
                f"?filter=annotation.run_id%3D%22{encoded_run_id}%22"
            )
            assert url == expected
        finally:
            tracing_mod._region = old_region
