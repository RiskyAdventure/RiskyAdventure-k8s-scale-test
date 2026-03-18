# Implementation Plan: OpenTelemetry Tracing

## Overview

Add opt-in OpenTelemetry tracing to the k8s-scale-test CLI, exporting to X-Ray via the OTLP endpoint. Implementation proceeds bottom-up: core tracing module first, then CLI wiring, then instrumentation of each subsystem, then tests.

## Tasks

- [x] 1. Create the core tracing module
  - [x] 1.1 Create `src/k8s_scale_test/tracing.py` with module-level state and OTLP exporter setup
    - Implement `init_tracing(aws_session, service_name)` that:
      - Creates `OTLPSpanExporter` pointed at `https://xray.{region}.amazonaws.com/v1/traces` with SigV4 auth
      - Creates `TracerProvider` with `BatchSpanProcessor` and the AWS X-Ray ID generator (`AwsXRayIdGenerator`)
      - Instruments `threading` via `ThreadingInstrumentor().instrument()` for automatic context propagation into all OS threads and `ThreadPoolExecutor` workers
      - Instruments `botocore` via `BotocoreInstrumentor().instrument()` for automatic AWS API call spans
      - Returns `True`/`False`
    - Implement `shutdown(timeout)` that calls `TracerProvider.shutdown()` with timeout guard
    - Implement `get_trace_url(run_id)` that returns the CloudWatch X-Ray console deep link
    - All functions return no-ops or safe defaults when `_enabled` is `False`
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 1.6, 7.1, 7.3, 7.4_

  - [x] 1.2 Implement `phase_span(phase_name, **attributes)` context manager
    - Create a root span named `scale-test-{phase_name}` using `tracer.start_as_current_span()`
    - Apply all attribute kwargs to the span via `span.set_attribute()`
    - On normal exit: span auto-closes with duration
    - On exception: set span status to ERROR, record exception, re-raise
    - No-op yield when `_enabled` is `False`
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 1.3 Implement `span(name, **attributes)` context manager
    - Create a child span under the current active span using `tracer.start_as_current_span()`
    - Store all attribute kwargs on the span
    - On exception: set status to ERROR, record exception, re-raise
    - No-op yield when `_enabled` is `False`
    - _Requirements: 3.3, 4.2, 4.3, 4.4, 6.1_

  - [x] 1.4 Implement `trace_thread(thread_name)` decorator
    - On thread entry: create a child span named `thread/{thread_name}` using `tracer.start_as_current_span()`
    - On thread exit: span auto-closes
    - Context propagation into the thread is handled automatically by the threading instrumentor — the decorator only needs to create the span, not manage context
    - No-op when `_enabled` is `False`
    - _Requirements: 3.1, 3.4, 8.2, 8.4_

  - [x] 1.5 Implement `install_slow_callback_monitor(loop, threshold_ms)` 
    - Use a lightweight periodic `call_later` probe approach: schedule a callback every 50ms, measure if it fires more than `threshold_ms` late (indicating the loop was blocked). Do NOT use `loop.set_debug(True)` — it wraps every callback and has significant overhead during scale tests.
    - Record detected blockages as spans with the delay duration as an attribute
    - _Requirements: 5.1, 5.2_

  - [x] 1.6 Write property tests for the tracing module
    - **Property 1: Phase span lifecycle** — generate random phase names, verify span name is `scale-test-{name}` and duration >= 0
    - **Validates: Requirements 2.1, 2.2**
    - **Property 2: Span attributes** — generate random attribute dicts, verify all present on span
    - **Validates: Requirements 2.3, 6.3**
    - **Property 3: Child span naming convention** — generate random prefix/name pairs, verify name is `{prefix}/{name}`
    - **Validates: Requirements 3.1, 4.2, 6.1**
    - **Property 4: Span attribute recording** — generate random attribute dicts, verify all present
    - **Validates: Requirements 4.3, 4.4**
    - **Property 5: Trace URL format** — generate random run_id strings, verify URL matches expected pattern
    - **Validates: Requirements 7.4**

  - [x] 1.7 Write property tests for concurrency and context propagation
    - **Property 6: Concurrent span safety** — spawn N threads each creating M spans, verify total count = N × M
    - **Validates: Requirements 8.1**
    - **Property 7: Trace context propagation** — verify child threads via `threading.Thread` (with threading instrumentor) share parent trace_id
    - **Validates: Requirements 8.2**

- [x] 2. Wire tracing into the CLI and TestConfig
  - [x] 2.1 Add `enable_tracing: bool = False` field to `TestConfig` in `models.py`
    - _Requirements: 1.1, 1.2_

  - [x] 2.2 Add `--enable-tracing` flag to `_add_run_args()` in `cli.py`
    - Pass the flag value into `TestConfig`
    - _Requirements: 1.1, 1.2_

  - [x] 2.3 Add tracing init/shutdown to `main()` in `cli.py`
    - After `_make_aws_session()`: if `enable_tracing`, call `init_tracing(aws_client)`
    - After `asyncio.run()`: call `shutdown()` and print trace URL
    - In the `KeyboardInterrupt` handler: call `shutdown()` before `sys.exit(130)`
    - _Requirements: 1.1, 1.3, 1.6, 7.1, 7.2, 7.4_

  - [x] 2.4 Write unit tests for CLI tracing integration
    - Test `--enable-tracing` flag is parsed correctly
    - Test tracing is not initialized when flag is absent
    - Test graceful handling when init_tracing returns False
    - _Requirements: 1.1, 1.2, 1.6_

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Instrument the Controller lifecycle phases
  - [x] 4.1 Wrap Controller phases with `phase_span` in `controller.py`
    - Wrap preflight phase in `phase_span("preflight", run_id=run_id, target_pods=...)`
    - Wrap scaling phase in `phase_span("scaling", ...)`
    - Wrap hold-at-peak phase in `phase_span("hold-at-peak", ...)`
    - Wrap cleanup phase in `phase_span("cleanup", ...)`
    - Install slow callback monitor on the asyncio loop at the start of `run()`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 5.1_

  - [x] 4.2 Write unit tests for Controller phase instrumentation
    - Verify phase spans are created and closed for each phase
    - Verify error in a phase marks span status as ERROR
    - _Requirements: 2.1, 2.2, 2.4_

- [x] 5. Instrument the Monitor threads and ticker
  - [x] 5.1 Add `trace_thread` decorator to watch thread methods and investigation span to `_safe_callback` in `monitor.py`
    - Decorate `_watch_deployments` with `@trace_thread("watch/deployments")`
    - Decorate `_watch_nodes` with `@trace_thread("watch/nodes")`
    - Decorate `_ticker_thread` with `@trace_thread("ticker")` and wrap each tick iteration in `span("monitor/tick")`
    - Add `span("anomaly/investigation", alert_type=...)` inside `_safe_callback`'s `_run_in_thread` closure. Context propagation into the closure thread is handled automatically by the threading instrumentor — no explicit `get_trace_context()`/`set_trace_context()` needed.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 8.2_

  - [x] 5.2 Write unit tests for Monitor tracing
    - Verify watch thread spans are created with correct names
    - Verify ticker spans are created per tick
    - Verify `_safe_callback` investigation span shares parent trace_id (threading instrumentor test)
    - _Requirements: 3.1, 3.3, 3.4_

- [x] 6. Instrument the Anomaly Detector investigation pipeline
  - [x] 6.1 Add spans to `handle_alert` in `anomaly.py`
    - Wrap the alert handler in `span("alert/{alert_type}", ...)`
    - Wrap each investigation layer (K8s events, pod phases, stuck nodes, ENI, SSM) in `span("investigation/{layer}")`
    - Set attributes on the parent span with root cause and severity on completion
    - _Requirements: 3.4, 6.1, 6.2, 6.3_

  - [x] 6.2 Write unit tests for Anomaly Detector tracing
    - Verify investigation layer spans are created
    - Verify root cause attribute is set
    - _Requirements: 6.1, 6.2, 6.3_

- [x] 7. Instrument the Observability Scanner and Health Sweep
  - [x] 7.1 Add spans to query execution in `observability.py`
    - Wrap PromQL queries in `span("promql/query", query=query_str)`
    - Wrap CloudWatch Insights queries in `span("cloudwatch/insights_query", query=query_str)`
    - _Requirements: 4.3, 4.4_

  - [x] 7.2 Add manual PromQL spans in `health_sweep.py`
    - Wrap `AMPMetricCollector._query_promql` in `span("promql/query", query=query_str)` — required because AMP PromQL queries use raw `urllib.request` with SigV4 signing, NOT boto3, so they are NOT auto-captured by the botocore instrumentor
    - Wrap K8s API calls (node listing, condition checks) in `span("k8s/{operation}")`
    - boto3 calls (SSM, EC2) are already auto-captured by the botocore instrumentor
    - `run_in_executor` calls do NOT need explicit wrappers — the threading instrumentor patches `ThreadPoolExecutor` to propagate context automatically
    - _Requirements: 4.1, 4.2, 4.3_

  - [x] 7.3 Write unit tests for Scanner and Health Sweep tracing
    - Verify PromQL query spans contain query attributes
    - Verify K8s operation spans are named correctly
    - Verify AMP `_query_promql` creates manual spans (not relying on botocore instrumentor)
    - Verify `run_in_executor` calls inherit parent context (threading instrumentor test)
    - _Requirements: 4.2, 4.3, 4.4, 8.2_

- [x] 8. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Dependencies: `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, `opentelemetry-instrumentation-threading`, `opentelemetry-instrumentation-botocore`, `amazon-opentelemetry-distro`
- All tracing is no-op when `--enable-tracing` is not passed — zero overhead in normal usage
- boto3 AWS calls (SSM, EC2, CloudWatch) are auto-instrumented by the botocore instrumentor; K8s and PromQL calls need manual spans
- AMP PromQL queries use raw `urllib.request` with SigV4 signing — they are NOT captured by the botocore instrumentor and require explicit `span()` wrapping
- The `opentelemetry-instrumentation-threading` package patches `threading.Thread`, `threading.Timer`, and `concurrent.futures.ThreadPoolExecutor` to copy `contextvars.Context` at construction time. This solves ALL 7 threading patterns in the codebase automatically.
- No custom emitter needed — the OTLP exporter sends directly to the X-Ray OTLP endpoint with SigV4 auth
- No `get_trace_context()`/`set_trace_context()` helpers needed — the threading instrumentor handles all context propagation
- No `trace_executor()` wrapper needed — `ThreadPoolExecutor` is patched by the threading instrumentor
- `BatchSpanProcessor` handles incremental flushing automatically — no custom flush logic needed
- Property tests use Hypothesis with `InMemorySpanExporter` for span capture
