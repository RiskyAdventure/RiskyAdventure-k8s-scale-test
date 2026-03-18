# Next Session: Investigation Pipeline Improvements

Read `.kiro/steering/engineering-rigor.md` before starting any work. Follow it rigorously — form testable hypotheses, negatively test them, read callers/tests/config before changing code, and validate findings before implementing.

## Project Context

This is a Kubernetes scale test tool (`src/k8s_scale_test/`) that scales 30K pods on EKS and monitors the scaling process. It has six monitoring modules that run concurrently during tests:

- `monitor.py` — PodRateMonitor: deployment watch-based pod ready rate tracking
- `anomaly.py` — AnomalyDetector: reactive investigation when rate drops are detected
- `observability.py` — ObservabilityScanner: proactive AMP/CloudWatch scanning (12 queries)
- `health_sweep.py` — HealthSweepAgent: per-node health at hold-at-peak
- `events.py` — EventWatcher: K8s warning event streaming
- `controller.py` — Observer: independent pod count cross-check

Read `docs/architecture.md`, `docs/anomaly-detection.md`, and `docs/observability-scanner.md` for full design context.

## Current State

- 237 tests pass (`pytest tests/ --ignore=tests/test_controller_observability_prompt.py`)
- 7 tests fail in `test_controller_observability_prompt.py` — these expect observability preflight gating that was never wired into the controller
- Tracing is enabled by default (`--enable-tracing`), exports to X-Ray via ADOT `OTLPAwsSpanExporter` with SigV4
- The `enforce-rigor` hook in `.kiro/hooks/` fires before every write operation

## Three Features to Build

### 1. Skeptical Review — Independent Finding Re-verification

**Problem:** The `AnomalyDetector` writes findings to `findings/*.json` but nothing independently verifies them. A wrong early conclusion can propagate through the investigation chain unchallenged.

**What to build:** A `FindingReviewer` class (new file `src/k8s_scale_test/reviewer.py`) that:
- Takes a finding JSON and the AMP/CloudWatch clients
- Identifies the central claim in the finding
- Re-queries the data sources using a different approach (e.g., if the investigation used a range query, try an instant query at the peak timestamp)
- Checks for staleness (conditions may have changed)
- Assigns confidence: high/medium/low
- Lists alternative explanations
- Writes checkpoint questions if confidence < high
- Appends a `review` field to the finding JSON

**Integration point:** Call from `controller.py` after each `anomaly.handle_alert()` returns a finding. The review should run asynchronously so it doesn't block the scaling loop.

**Schema:** The `review` field structure is already defined in `.kiro/steering/run-scale-test.md` under "Skeptical Review Process".

**Tests:** Write tests in `tests/test_reviewer.py`. Use `inspect.getsource()` pattern (like `test_controller_tracing.py`) for integration checks, plus unit tests with mock AMP responses.

### 2. AMP Integration in Reactive Investigation

**Problem:** The `AnomalyDetector` investigates rate drops using K8s events, pod phases, stuck nodes, EC2 ENI checks, and SSM diagnostics — but it never queries AMP/Prometheus metrics. The `ObservabilityScanner` queries AMP proactively but doesn't share its findings with the anomaly detector.

**What to build:** Add an AMP investigation step to `AnomalyDetector.handle_alert()`:
- After the K8s event collection step, query AMP for node-level metrics around the alert timestamp (±3 min)
- Check: CPU/memory pressure on affected nodes, network error rates, IPAMD metrics, pod restart counts
- Use the `AMPMetricCollector` from `health_sweep.py` (it already handles SigV4 signing and retries)
- Add the AMP evidence to the finding's `evidence` list

**Key constraint:** The `AnomalyDetector.__init__` doesn't currently receive AMP config. You'll need to pass `amp_workspace_id` and `aws_profile` through, or pass a pre-configured `AMPMetricCollector` instance.

**Tests:** Add tests in `tests/test_anomaly.py` (existing file). Check that AMP queries are attempted when `amp_workspace_id` is configured, and gracefully skipped when not.

### 3. Cross-Source Correlation via Shared Context

**Problem:** The `ObservabilityScanner` and `AnomalyDetector` work independently. The scanner might detect CPU pressure proactively, but when a rate drop fires 30s later, the anomaly detector starts from scratch without knowing the scanner already found the cause.

**What to build:** A lightweight shared context object that both modules can read/write:
- Scanner writes its findings (query_name, severity, affected nodes, timestamp)
- Anomaly detector reads scanner findings before starting its investigation
- If a scanner finding matches the alert's symptoms (same time window, overlapping affected nodes), the anomaly detector references it instead of re-investigating
- The `ContextFileWriter` (`agent_context.py`) already writes `finding_summaries` — extend this or create a new in-memory shared state

**Design consideration:** The scanner runs in an asyncio task, the anomaly detector runs in the event loop via callbacks. Thread safety matters — use a lock or asyncio-safe data structure.

**Tests:** Property-based test: generate random scanner findings and alerts, verify the anomaly detector correctly matches or ignores them based on time window and affected resources.

## Build Order

1. Start with Feature 2 (AMP in anomaly detector) — it's the most contained change
2. Then Feature 3 (shared context) — it connects the scanner and anomaly detector
3. Then Feature 1 (skeptical review) — it builds on top of the richer findings from Features 2+3

## Key Files to Read First

- `src/k8s_scale_test/anomaly.py` — the reactive investigation pipeline
- `src/k8s_scale_test/observability.py` — the proactive scanner
- `src/k8s_scale_test/health_sweep.py` — has `AMPMetricCollector` you'll reuse
- `src/k8s_scale_test/agent_context.py` — the shared context writer
- `src/k8s_scale_test/models.py` — all data models (Finding, Alert, ScanResult)
- `.kiro/steering/run-scale-test.md` — the full runbook including finding schema and review process
- `tests/test_anomaly.py`, `tests/test_observability.py` — existing test patterns

## Running Tests

```bash
# All tests (expect 7 failures in test_controller_observability_prompt.py)
python3 -m pytest tests/ -v

# Skip known failures
python3 -m pytest tests/ --ignore=tests/test_controller_observability_prompt.py -v

# Just the files you're changing
python3 -m pytest tests/test_anomaly.py tests/test_observability.py -v
```
