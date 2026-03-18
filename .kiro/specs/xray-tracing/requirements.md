# Requirements Document

## Introduction

Add opt-in distributed tracing to the k8s-scale-test CLI tool so that operators can visualize the full test lifecycle — phase transitions, thread activity, blocking AWS/K8s API calls, and asyncio event loop utilization — in the CloudWatch X-Ray console. The tool runs locally on macOS (not as a Lambda or container service).

The implementation uses **OpenTelemetry** (the CNCF standard) with the **ADOT (AWS Distro for OpenTelemetry) SDK** to send traces directly to the X-Ray OTLP endpoint — no collector sidecar or X-Ray daemon required. This is the AWS-recommended approach following the X-Ray SDK's entry into maintenance mode on February 25, 2026.

## Glossary

- **Tracing_Module**: The new `tracing.py` module that initializes and manages the OpenTelemetry tracing lifecycle (SDK setup, span creation, shutdown).
- **Span**: An OpenTelemetry trace unit representing an operation. Root spans represent major test phases; child spans represent individual operations.
- **Trace_Context**: The `contextvars.ContextVar`-based storage that propagates the current span across threads and coroutines. OpenTelemetry Python uses `contextvars` (not thread-locals), which propagate automatically into `asyncio` tasks but NOT into OS threads.
- **Controller**: The `ScaleTestController` class that orchestrates the full test lifecycle.
- **Monitor**: The `PodRateMonitor` class that runs background watch threads and a ticker thread.
- **Anomaly_Detector**: The `AnomalyDetector` class that investigates alerts via a multi-layer evidence pipeline.
- **Health_Sweep_Agent**: The `HealthSweepAgent` class that samples node health at peak load.
- **Observability_Scanner**: The `ObservabilityScanner` class that runs periodic PromQL and CloudWatch queries.
- **CLI**: The `cli.py` entry point that parses arguments and launches the Controller.

## Threading Model Summary

The application has 7 distinct threading patterns that tracing must handle:

1. **Watch threads** — long-lived `threading.Thread` for K8s deployment/node watches (monitor.py). One per namespace + one for nodes.
2. **Ticker thread** — long-lived `threading.Thread` running a 5s sampling loop with `time.sleep()` (monitor.py).
3. **`_safe_callback` closure threads** — short-lived `threading.Thread` spawned from `_safe_callback`, each creating its own `asyncio.new_event_loop()` to run the anomaly detector (monitor.py). The thread target is an anonymous closure, not a decorated method.
4. **`run_in_executor` SSM lambdas** — `loop.run_in_executor(None, lambda: ssm.send_command(...))` for non-blocking boto3 calls (diagnostics.py). Uses the default `ThreadPoolExecutor`.
5. **`run_in_executor` SSM polling** — `loop.run_in_executor(None, _execute)` where `_execute` is a local function doing a 30s synchronous SSM send+poll loop (diagnostics.py).
6. **`run_in_executor` AMP HTTP** — `loop.run_in_executor(None, self._do_http, req)` for blocking `urllib.request` calls with SigV4 signing (health_sweep.py). NOT boto3.
7. **`run_in_executor` K8s API** — `loop.run_in_executor(None, lambda: v1.list_node(...))` for blocking K8s client calls (health_sweep.py).

OpenTelemetry's `contextvars`-based context does NOT automatically propagate into any of these OS threads. The `opentelemetry-instrumentation-threading` package solves patterns 1-3 by monkey-patching `threading.Thread` and `ThreadPoolExecutor` to copy context on thread creation. Patterns 4-7 are covered by the same `ThreadPoolExecutor` patch.

## Requirements

### Requirement 1: Opt-In Tracing Activation

**User Story:** As an operator, I want to enable tracing via a CLI flag, so that tracing overhead is only incurred when I need to troubleshoot.

#### Acceptance Criteria

1. WHEN the `--enable-tracing` flag is passed to the `run` subcommand, THE CLI SHALL initialize the Tracing_Module before creating the Controller.
2. WHEN the `--enable-tracing` flag is absent, THE CLI SHALL skip all tracing initialization and the application SHALL behave identically to the untraced version.
3. WHEN tracing is enabled, THE Tracing_Module SHALL configure the OpenTelemetry SDK with the OTLP/HTTP exporter pointed at the X-Ray OTLP endpoint (`https://xray.{region}.amazonaws.com/v1/traces`), using SigV4 authentication via the active boto3 session credentials (profile `shancor+test-Admin`, region `us-west-2`).
4. WHEN tracing is enabled, THE Tracing_Module SHALL instrument the `threading` module via `opentelemetry-instrumentation-threading` so that trace context propagates automatically into all OS threads and `ThreadPoolExecutor` workers.
5. WHEN tracing is enabled, THE Tracing_Module SHALL instrument the `boto3` library via `opentelemetry-instrumentation-botocore` so that all subsequent AWS API calls (SSM, EC2, CloudWatch) are automatically captured as child spans. Note: AMP PromQL queries use raw `urllib.request` with SigV4 signing, not boto3, so they are NOT auto-captured and require manual spans.
6. IF the OpenTelemetry SDK initialization fails, THEN THE Tracing_Module SHALL log a warning and allow the test to proceed without tracing.

### Requirement 2: Phase-Level Trace Spans

**User Story:** As an operator, I want each test lifecycle phase to appear as a distinct trace span, so that I can see phase durations and transitions in the X-Ray service map.

#### Acceptance Criteria

1. WHEN the Controller enters a lifecycle phase (preflight, scaling, hold-at-peak, cleanup), THE Tracing_Module SHALL create a new root span named `scale-test-{phase}`.
2. WHEN the Controller exits a lifecycle phase, THE Tracing_Module SHALL close the corresponding span with the phase duration recorded.
3. THE Tracing_Module SHALL set attributes on each span including `run_id`, `target_pods`, and `phase_name`.
4. IF a phase ends due to an error, THEN THE Tracing_Module SHALL mark the span status as ERROR and record the exception.

### Requirement 3: Thread Activity Tracing

**User Story:** As an operator, I want to see which background threads are active and for how long, so that I can identify thread starvation or deadlocks.

#### Acceptance Criteria

1. WHEN the Monitor starts a watch thread (deployment watch, node watch), THE Tracing_Module SHALL create a child span named `thread/{thread_name}` under the active phase span.
2. WHEN a watch thread reconnects after a disconnect, THE Tracing_Module SHALL create a new child span for the reconnection attempt.
3. WHEN the Monitor ticker loop completes a tick, THE Tracing_Module SHALL record the tick duration as a child span named `monitor/tick`.
4. WHEN the Anomaly_Detector investigation callback runs in a new thread via `_safe_callback`, THE Tracing_Module SHALL create a child span named `anomaly/investigation` linked to the alert that triggered it. The `opentelemetry-instrumentation-threading` package handles context propagation into the `_safe_callback` closure thread automatically.

### Requirement 4: Blocking Call Tracing

**User Story:** As an operator, I want to see the latency of every blocking AWS and Kubernetes API call, so that I can identify slow external dependencies.

#### Acceptance Criteria

1. WHEN tracing is enabled, THE Tracing_Module SHALL automatically capture all boto3 API calls (SSM SendCommand, EC2 DescribeNetworkInterfaces, CloudWatch StartQuery) as child spans via the `opentelemetry-instrumentation-botocore` instrumentor. Note: AMP PromQL queries use raw `urllib.request` with SigV4 signing and are NOT captured by the botocore instrumentor.
2. WHEN the Anomaly_Detector or Health_Sweep_Agent makes a Kubernetes API call via `run_in_executor`, THE Tracing_Module SHALL wrap the call in a child span named `k8s/{api_operation}`. Context propagation into the executor thread is handled by `opentelemetry-instrumentation-threading`.
3. WHEN the Observability_Scanner or Health_Sweep_Agent executes a PromQL query via `urllib.request`, THE Tracing_Module SHALL record the query as a manual child span named `promql/query` with the query string as an attribute. This is required because the botocore instrumentor does not capture raw urllib calls.
4. WHEN the Observability_Scanner executes a CloudWatch Logs Insights query, THE Tracing_Module SHALL record the query as a child span named `cloudwatch/insights_query` with the query string as an attribute.

### Requirement 5: Asyncio Event Loop Instrumentation

**User Story:** As an operator, I want to see asyncio event loop utilization, so that I can detect when the loop is blocked by synchronous code.

#### Acceptance Criteria

1. WHILE tracing is enabled, THE Tracing_Module SHALL install a lightweight slow-callback monitor on the asyncio event loop using a custom `call_later`-based sampling approach (NOT `loop.set_debug(True)`, which has excessive overhead for production scale tests). The monitor SHALL record any callback exceeding 100ms as a span named `asyncio/slow_callback`.
2. WHEN a slow callback is detected, THE Tracing_Module SHALL set attributes on the span with the callback source (coroutine name or function name) and the actual duration.

### Requirement 6: Alert-to-Investigation Pipeline Tracing

**User Story:** As an operator, I want to trace the full path from a monitor alert through the anomaly investigation pipeline, so that I can measure investigation latency and identify bottlenecks.

#### Acceptance Criteria

1. WHEN the Monitor fires an alert, THE Tracing_Module SHALL create a child span named `alert/{alert_type}` with the alert details as attributes.
2. WHEN the Anomaly_Detector begins an investigation, THE Tracing_Module SHALL create a child span for each investigation layer (K8s events, pod phases, stuck nodes, ENI state, SSM diagnostics).
3. WHEN the investigation completes, THE Tracing_Module SHALL set attributes on the parent span with the root cause string and severity.

### Requirement 7: Trace Data Export and Shutdown

**User Story:** As an operator, I want all trace data flushed to X-Ray before the process exits, so that I can view complete traces in the CloudWatch console.

#### Acceptance Criteria

1. WHEN the Controller run completes (success or failure), THE Tracing_Module SHALL flush all pending trace data via the OTLP exporter.
2. WHEN the process receives a KeyboardInterrupt (SIGINT), THE Tracing_Module SHALL flush pending traces before exiting.
3. THE Tracing_Module SHALL use the `BatchSpanProcessor` which flushes spans incrementally during the run (not only at shutdown). The final shutdown flush SHALL complete within 10 seconds to avoid delaying process exit.
4. WHEN tracing is enabled, THE CLI SHALL print the X-Ray trace URL (console deep link) for the run after flush completes.

### Requirement 8: Thread Safety and Performance

**User Story:** As an operator, I want tracing to be safe for concurrent use and have minimal performance impact, so that it does not interfere with the 5-second ticker cadence or test accuracy.

#### Acceptance Criteria

1. THE Tracing_Module SHALL support concurrent span creation from multiple OS threads without data races or corruption. OpenTelemetry's `TracerProvider` and `BatchSpanProcessor` are thread-safe by design.
2. THE Tracing_Module SHALL propagate trace context from parent threads to child threads via `opentelemetry-instrumentation-threading`, which patches `threading.Thread`, `threading.Timer`, and `concurrent.futures.ThreadPoolExecutor` to copy `contextvars` context on thread creation. For the Monitor's `_safe_callback` pattern (which spawns threads with closures that create their own `asyncio.new_event_loop()`), the threading instrumentor handles context propagation automatically since it patches `threading.Thread.__init__` to capture context at construction time.
3. WHILE tracing is enabled, THE Monitor ticker loop SHALL maintain its 5-second cadence with no more than 50ms of additional latency per tick attributable to tracing.
4. THE Tracing_Module SHALL use OpenTelemetry's `contextvars`-based context storage, which is inherently thread-safe and propagates automatically into asyncio tasks.
