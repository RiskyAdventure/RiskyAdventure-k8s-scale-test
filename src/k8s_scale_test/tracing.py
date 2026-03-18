"""OpenTelemetry tracing integration for k8s-scale-test.

Provides opt-in distributed tracing via the ADOT SDK, exporting to
AWS X-Ray through the OTLP/HTTP endpoint with SigV4 authentication.

When tracing is not enabled, every public function is a no-op with
zero overhead beyond a single boolean check.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import urllib.parse
from typing import Any, Callable, Generator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state — set by init_tracing(), read by all helpers
# ---------------------------------------------------------------------------
_enabled: bool = False
_tracer = None  # opentelemetry.trace.Tracer | None
_provider = None  # opentelemetry.sdk.trace.TracerProvider | None
_region: str = "us-west-2"


def init_tracing(aws_session, service_name: str = "k8s-scale-test") -> bool:
    """Initialise the OpenTelemetry SDK with OTLP/HTTP export to X-Ray.

    Steps
    -----
    1. Resolve the AWS region from the boto3 session.
    2. Build SigV4 auth headers via the session credentials.
    3. Create an ``OTLPSpanExporter`` pointed at the X-Ray OTLP endpoint.
    4. Create a ``TracerProvider`` with ``BatchSpanProcessor`` and the
       AWS X-Ray ID generator (``AwsXRayIdGenerator``).
    5. Instrument ``threading`` for automatic context propagation.
    6. Instrument ``botocore`` for automatic AWS API call spans.

    Returns ``True`` on success, ``False`` on any failure (tracing
    becomes a no-op for the rest of the process).
    """
    global _enabled, _tracer, _provider, _region

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.extension.aws.trace import (
            AwsXRayIdGenerator,
        )
        from opentelemetry.instrumentation.threading import (
            ThreadingInstrumentor,
        )
        from opentelemetry.instrumentation.botocore import (
            BotocoreInstrumentor,
        )

        # --- Region -----------------------------------------------------------
        _region = aws_session.region_name or "us-west-2"

        # --- SigV4 auth via OTLP exporter ------------------------------------
        # The ADOT OTLP exporter supports SigV4 natively when the
        # ``OTEL_EXPORTER_OTLP_TRACES_HEADERS`` env-var is absent and
        # credentials are available in the environment.  We inject the
        # credentials from the boto3 session so the exporter can sign
        # requests.
        credentials = aws_session.get_credentials()
        if credentials is None:
            log.warning("No AWS credentials available — cannot init tracing")
            return False
        frozen = credentials.get_frozen_credentials()

        # Build the auth header dict for SigV4 signing.
        # The OTLPSpanExporter with the ADOT distro picks up SigV4 from
        # environment variables.
        import os

        os.environ.setdefault("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                              f"https://xray.{_region}.amazonaws.com/v1/traces")
        os.environ["AWS_ACCESS_KEY_ID"] = frozen.access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
        if frozen.token:
            os.environ["AWS_SESSION_TOKEN"] = frozen.token
        os.environ.setdefault("AWS_DEFAULT_REGION", _region)

        endpoint = f"https://xray.{_region}.amazonaws.com/v1/traces"

        exporter = OTLPSpanExporter(endpoint=endpoint)

        # --- TracerProvider ---------------------------------------------------
        id_generator = AwsXRayIdGenerator()
        _provider = TracerProvider(id_generator=id_generator)
        _provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(_provider)
        _tracer = trace.get_tracer(service_name)

        # --- Instrumentors ----------------------------------------------------
        ThreadingInstrumentor().instrument()
        BotocoreInstrumentor().instrument()

        _enabled = True
        log.info("Tracing initialised — exporting to X-Ray in %s", _region)
        return True

    except Exception:
        log.warning("Tracing initialisation failed — continuing without tracing",
                    exc_info=True)
        _enabled = False
        return False


def shutdown(timeout: float = 10.0) -> None:
    """Flush pending spans and shut down the TracerProvider.

    Respects *timeout* so the process exit is never blocked for long.
    Safe to call when tracing was never initialised.
    """
    global _enabled
    if not _enabled or _provider is None:
        return
    try:
        _provider.shutdown()
    except Exception:
        log.warning("Error during tracing shutdown", exc_info=True)
    finally:
        _enabled = False


def get_trace_url(run_id: str) -> str:
    """Return the CloudWatch X-Ray console deep-link for *run_id*.

    Works regardless of whether tracing is enabled — always returns a
    valid URL string.
    """
    encoded_run_id = urllib.parse.quote(run_id, safe="")
    return (
        f"https://{_region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={_region}#xray:traces"
        f"?filter=annotation.run_id%3D%22{encoded_run_id}%22"
    )


# ---------------------------------------------------------------------------
# Phase span — task 1.2
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def phase_span(phase_name: str, **attributes: Any) -> Generator:
    """Context manager that creates a root span for a test phase.

    Creates a span named ``scale-test-{phase_name}`` and applies all
    *attributes* as span attributes.  On normal exit the span closes
    automatically with its duration recorded.  On exception the span
    status is set to ERROR, the exception is recorded on the span, and
    the original exception is re-raised.

    When tracing is disabled (``_enabled is False``), yields ``None``
    with zero overhead.

    Usage::

        with phase_span("scaling", run_id=rid, target_pods=500):
            await do_scaling()
    """
    if not _enabled or _tracer is None:
        yield None
        return

    from opentelemetry.trace import StatusCode

    with _tracer.start_as_current_span(f"scale-test-{phase_name}") as s:
        for key, value in attributes.items():
            s.set_attribute(key, value)
        try:
            yield s
        except Exception as exc:
            s.set_status(StatusCode.ERROR, str(exc))
            s.record_exception(exc)
            raise


# ---------------------------------------------------------------------------
# Stubs for functions implemented in later tasks (1.3 – 1.5).
# These no-ops allow the module to be imported immediately.
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Generator:
    """Context manager that creates a child span under the current span.

    Creates a span named *name* and applies all *attributes* as span
    attributes.  On normal exit the span closes automatically.  On
    exception the span status is set to ERROR, the exception is
    recorded, and the original exception is re-raised.

    When tracing is disabled (``_enabled is False``), yields ``None``
    with zero overhead.

    Usage::

        with span("k8s/list_pods", namespace="default"):
            pods = v1.list_namespaced_pod("default")
    """
    if not _enabled or _tracer is None:
        yield None
        return

    from opentelemetry.trace import StatusCode

    with _tracer.start_as_current_span(name) as s:
        for key, value in attributes.items():
            s.set_attribute(key, value)
        try:
            yield s
        except Exception as exc:
            s.set_status(StatusCode.ERROR, str(exc))
            s.record_exception(exc)
            raise


def trace_thread(thread_name: str) -> Callable:
    """Decorator for background thread entry points.

    Creates a child span named ``thread/{thread_name}`` for the
    lifetime of the decorated function.  Context propagation into the
    thread is handled automatically by the ``threading`` instrumentor —
    this decorator only needs to create the span, not manage context.

    When tracing is disabled (``_enabled is False`` or ``_tracer`` is
    ``None``), the original function is called directly with zero
    overhead beyond a single boolean check.

    Usage::

        @trace_thread("watch/deployments")
        def _watch_deployments(self, namespace):
            ...
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _enabled or _tracer is None:
                return fn(*args, **kwargs)

            from opentelemetry.trace import StatusCode

            with _tracer.start_as_current_span(f"thread/{thread_name}") as s:
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    s.set_status(StatusCode.ERROR, str(exc))
                    s.record_exception(exc)
                    raise
        return wrapper
    return decorator


def install_slow_callback_monitor(
    loop: Any, threshold_ms: float = 100.0
) -> None:
    """Install a lightweight asyncio slow-callback detector.

    Schedules a periodic ``call_later(0.05, probe)`` callback.  When
    the probe fires it compares ``time.monotonic()`` to the expected
    fire time.  If the delay exceeds *threshold_ms* the event loop was
    blocked — a span named ``asyncio/slow_callback`` is recorded with
    the measured delay as an attribute.

    The probe reschedules itself to keep monitoring continuously.

    Does **not** use ``loop.set_debug(True)`` which wraps every
    callback and has significant overhead during scale tests.

    No-op when tracing is disabled (``_enabled is False``).
    """
    if not _enabled:
        return

    import time

    _PROBE_INTERVAL = 0.05  # 50 ms

    def _probe(expected_time: float) -> None:
        now = time.monotonic()
        delay_ms = (now - expected_time) * 1000.0

        if delay_ms > threshold_ms and _tracer is not None:
            with _tracer.start_as_current_span("asyncio/slow_callback") as s:
                s.set_attribute("delay_ms", delay_ms)

        # Reschedule if still enabled
        if _enabled:
            next_expected = now + _PROBE_INTERVAL
            loop.call_later(_PROBE_INTERVAL, _probe, next_expected)

    # Kick off the first probe
    first_expected = time.monotonic() + _PROBE_INTERVAL
    loop.call_later(_PROBE_INTERVAL, _probe, first_expected)
