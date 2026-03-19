# Next Session: Event-Driven Investigation Triggers

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

## Problem Statement

The anomaly detector only triggers investigations on pod ready rate drops (detected by PodRateMonitor). K8s warning events are recorded by the EventWatcher to `events.jsonl` but never analyzed proactively. This creates a blind spot: thousands of `Failed`, `FailedCreatePodSandBox`, or `FailedCreatePodContainer` events can accumulate without triggering an investigation if pods eventually succeed on retry and the rate doesn't drop measurably.

Evidence from the 2026-03-18 run: 16,999 warning events (3,059 `Failed`, 1,249 `FailedCreatePodSandBox`, 20 `FailedCreatePodContainer`) but only 1 investigation triggered (by a rate drop). The chart now flags these as "⚠ Not investigated" but the underlying analysis gap remains.

## What Needs to Change

Add event-driven investigation triggers to the EventWatcher so that high-volume warning patterns cause the anomaly detector to investigate, independent of rate drops.

### Design Constraints

1. The `_alert_in_flight` mutex in PodRateMonitor prevents concurrent investigations (to avoid duplicate SSM calls). The EventWatcher trigger must respect this — either share the same mutex or use a separate lightweight investigation path that doesn't call SSM.

2. The EventWatcher currently runs in `events.py` and streams events to `events.jsonl`. It has no connection to the AnomalyDetector. The controller wires them up independently in Phase 5 (`controller.py` ~line 405).

3. Investigations are expensive (SSM calls, ENI checks, AMP queries). Event-triggered investigations should be throttled — e.g., at most 1 event-triggered investigation per 2 minutes, and only when a warning reason accumulates >100 events without an existing investigation covering it.

### Suggested Approach

1. Add an `on_alert` callback to EventWatcher (similar to PodRateMonitor's) that fires when a warning reason exceeds a configurable threshold (e.g., 100 events in 60 seconds).

2. In the controller, wire `event_watcher.on_alert(anomaly.handle_alert)` alongside the existing `monitor.on_alert(anomaly.handle_alert)`.

3. Create a new `AlertType.EVENT_SURGE` in models.py for event-triggered alerts. The alert context should include the warning reason, count, and sample messages.

4. The AnomalyDetector's `handle_alert` already handles different alert types — it should work with EVENT_SURGE alerts without major changes, since it collects K8s events in Layer 1 regardless of what triggered it.

5. Add a deduplication check: if the warning reason is already in an existing finding's evidence_references, skip the investigation.

### Key Files to Modify

- `src/k8s_scale_test/events.py` — add threshold-based alert triggering
- `src/k8s_scale_test/models.py` — add `AlertType.EVENT_SURGE`
- `src/k8s_scale_test/controller.py` — wire EventWatcher alerts to AnomalyDetector
- `src/k8s_scale_test/anomaly.py` — handle EVENT_SURGE alert type (may need dedup logic)

### Verification

- Run against existing test data in `scale-test-results/2026-03-18_11-34-04/` to verify that the `Failed` (3,059 events) and `FailedCreatePodSandBox` (1,249 events) reasons would have triggered investigations
- The chart's "Investigated?" column should show "✓ In findings" for all high-volume warning reasons after the fix
- All 244 existing tests must pass

## Recent Changes (This Session)

Several fixes were made to the investigation summary chart and analysis pipeline:

1. **Chart: Investigation Summary** — replaced the simple error count table with a comprehensive section showing anomaly findings (deduplicated root causes, evidence chains, investigation layers), proactive scanner results, K8s warning events with investigation status, and diagnostic collection notes.

2. **Anomaly: SSM-verified root causes** — the correlator no longer assumes MAC collision for FailedCreatePodSandBox. It checks SSM logs for the specific signature before labeling.

3. **Anomaly: Post-SSM KB check** — added a second KB lookup after SSM diagnostics are collected, so KB entries with log_patterns can match (the first check only has events).

4. **Scanner: Service-level error grouping** — changed the CloudWatch Insights query to group by systemd_unit and count distinct hosts instead of grouping by raw message. The chart extracts error patterns from both old and new formats.

5. **Chart: Uninvestigated event flagging** — cross-references warning events against investigation evidence and flags high-volume reasons that were never covered.

## Running Tests

```bash
python3 -m pytest tests/ -v
```

## Key Files

- `src/k8s_scale_test/anomaly.py` — the reactive investigation pipeline
- `src/k8s_scale_test/observability.py` — the proactive scanner
- `src/k8s_scale_test/events.py` — EventWatcher (event streaming, needs alert triggers)
- `src/k8s_scale_test/monitor.py` — PodRateMonitor (has the `_alert_in_flight` pattern to follow)
- `src/k8s_scale_test/controller.py` — wires everything together in Phase 5
- `src/k8s_scale_test/models.py` — Alert, AlertType, Finding data models
- `src/k8s_scale_test/chart.py` — investigation summary chart generation
- `src/k8s_scale_test/reviewer.py` — skeptical finding review
- `src/k8s_scale_test/shared_context.py` — cross-source correlation store
