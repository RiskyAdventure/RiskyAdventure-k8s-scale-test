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

All three features from the original plan have been implemented:

1. **Skeptical Review** (`reviewer.py`) — `FindingReviewer` independently re-verifies findings by re-querying AMP/CloudWatch with different approaches, checking for staleness, assigning confidence (high/medium/low), listing alternative explanations, and generating checkpoint questions. Integrated into the controller as async non-blocking tasks after each `anomaly.handle_alert()`.

2. **AMP Integration in Reactive Investigation** (`anomaly.py`) — The `AnomalyDetector` now accepts a pre-configured `AMPMetricCollector` instance and queries AMP for node-level metrics (CPU, memory, network errors, IPAMD metrics, pod restarts) during Layer 4.5 of the investigation pipeline. Threshold violations are added to evidence and root cause.

3. **Cross-Source Correlation via SharedContext** (`shared_context.py`) — The `ObservabilityScanner` writes findings to an in-memory `SharedContext`. The `AnomalyDetector` queries it in Layer 0 before starting its investigation pipeline. Matches are classified as "strong" (temporal + resource overlap) or "weak" (temporal only).

- Tracing is enabled by default (`--enable-tracing`), exports to X-Ray via ADOT `OTLPAwsSpanExporter` with SigV4
- The `enforce-rigor` hook in `.kiro/hooks/` fires before every write operation

## Running Tests

```bash
# All tests
python3 -m pytest tests/ -v

# Skip known failures
python3 -m pytest tests/ --ignore=tests/test_controller_observability_prompt.py -v

# Just specific modules
python3 -m pytest tests/test_anomaly.py tests/test_observability.py tests/test_reviewer.py -v
```

## Key Files

- `src/k8s_scale_test/anomaly.py` — the reactive investigation pipeline (includes AMP Layer 4.5)
- `src/k8s_scale_test/observability.py` — the proactive scanner
- `src/k8s_scale_test/reviewer.py` — skeptical finding review
- `src/k8s_scale_test/shared_context.py` — cross-source correlation store
- `src/k8s_scale_test/health_sweep.py` — has `AMPMetricCollector`
- `src/k8s_scale_test/agent_context.py` — the shared context writer
- `src/k8s_scale_test/models.py` — all data models (Finding, Alert, ScanResult)
- `.kiro/steering/run-scale-test.md` — the full runbook including finding schema and review process
