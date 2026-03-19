# Next Session: Post-Scaling Event Investigation

Read `.kiro/steering/engineering-rigor.md` before starting any work. Follow it rigorously — form testable hypotheses, negatively test them, read callers/tests/config before changing code, and validate findings before implementing.

## Project Context

This is a Kubernetes scale test tool (`src/k8s_scale_test/`) that scales 30K pods on EKS and monitors the scaling process. It has six monitoring modules that run concurrently during tests:

- `monitor.py` — PodRateMonitor: deployment watch-based pod ready rate tracking
- `anomaly.py` — AnomalyDetector: reactive investigation when rate drops are detected
- `observability.py` — ObservabilityScanner: proactive AMP/CloudWatch scanning (12 queries)
- `health_sweep.py` — HealthSweepAgent: per-node health at hold-at-peak
- `events.py` — EventWatcher: K8s warning event streaming to events.jsonl
- `controller.py` — Observer: independent pod count cross-check

Read `docs/architecture.md`, `docs/anomaly-detection.md`, and `docs/observability-scanner.md` for full design context.

## Problem Statement

The anomaly detector only triggers investigations on pod ready rate drops (detected by PodRateMonitor). K8s warning events are recorded by the EventWatcher to `events.jsonl` but never analyzed proactively. This creates a blind spot: thousands of `Failed`, `FailedCreatePodSandBox`, or `FailedCreatePodContainer` events can accumulate without triggering an investigation if pods eventually succeed on retry and the rate doesn't drop measurably.

Evidence from the 2026-03-18 run: 16,999 warning events (3,059 `Failed`, 1,249 `FailedCreatePodSandBox`, 20 `FailedCreatePodContainer`) but only 1 investigation triggered (by a rate drop). The chart flags these as "⚠ Not investigated" but the underlying analysis gap remains.

## Design: Hold-at-Peak Event Analysis

Run event-driven investigations during the hold-at-peak phase (Phase 7a in controller.py), after scaling completes but before cleanup. This is the right timing because:

1. During scaling, the `_alert_in_flight` mutex prevents concurrent investigations to avoid duplicate SSM calls that could interfere with node provisioning. Running event investigations during scaling would either block rate-drop investigations or require parallel SSM calls.

2. During hold-at-peak, the cluster is stable, the rate monitor is quiet (at target, no more rate drops), and there's already a dedicated window (configurable via `--hold-at-peak`, default 90s) where the health sweep runs. This is the natural place for post-scaling analysis.

3. The rate-drop trigger still covers the acute case during scaling (failures bad enough to slow the rate). The hold-at-peak event analysis catches the chronic case (failures that don't affect the rate but indicate underlying problems worth investigating).

### Trade-offs

- You lose real-time investigation of event surges during scaling. If `Failed` events pile up at minute 3 of a 10-minute scale, you won't get an investigation until hold-at-peak.
- If `hold_at_peak` is set to 0 or very short, event analysis won't have time to run. The implementation should enforce a minimum hold duration when event analysis is needed, or run it after the hold timer but before cleanup.
- SSM calls during hold-at-peak are safe (cluster is stable) but still take 10-30s per node. Cap at 3 nodes per investigation to keep total time under 2 minutes.

### Implementation Plan

#### 1. EventWatcher: Track warning reason counts (`events.py`)

Add an in-memory counter to EventWatcher that tracks warning events by reason during the test. No alert callback needed — just expose a method to query accumulated counts.

```python
def get_warning_summary(self) -> dict[str, dict]:
    """Return {reason: {count, first_seen, last_seen, sample_message}} for all warning reasons."""
```

#### 2. New AlertType (`models.py`)

Add `AlertType.EVENT_SURGE` for event-triggered alerts. The alert context should include the warning reason, count, and sample messages.

#### 3. Post-scaling event analysis (`controller.py`)

In Phase 7a (hold-at-peak), after the health sweep completes:

1. Get the EventWatcher's warning summary
2. Get the list of warning reasons already covered by existing findings (from `self._findings`)
3. For each uncovered reason with >100 events, create an `EVENT_SURGE` alert and run `anomaly.handle_alert()`
4. Run these sequentially (not concurrently) to avoid SSM contention
5. Cap at 3 event-surge investigations per run to bound the hold-at-peak extension

```python
# Phase 7a addition — after health sweep, before cleanup
uncovered = event_watcher.get_uncovered_reasons(self._findings, threshold=100)
for reason, info in uncovered[:3]:
    alert = Alert(
        alert_type=AlertType.EVENT_SURGE,
        timestamp=datetime.now(timezone.utc),
        message=f"Event surge: {reason} x{info['count']} (uninvestigated)",
        context={"reason": reason, "count": info["count"],
                 "sample": info["sample_message"], "namespaces": namespaces},
    )
    finding = await anomaly.handle_alert(alert)
    self._findings.append(finding)
```

#### 4. AnomalyDetector: Handle EVENT_SURGE (`anomaly.py`)

The existing `handle_alert` should work without major changes — it collects K8s events in Layer 1 regardless of alert type. But add a log line to distinguish event-surge investigations from rate-drop investigations in the output.

#### 5. Deduplication

Before creating an EVENT_SURGE alert, check if the warning reason already appears in any existing finding's `evidence_references`. The EventWatcher's `get_uncovered_reasons()` method should accept the findings list and filter out already-covered reasons.

### Key Files to Modify

- `src/k8s_scale_test/events.py` — add `get_warning_summary()` and `get_uncovered_reasons()`
- `src/k8s_scale_test/models.py` — add `AlertType.EVENT_SURGE`
- `src/k8s_scale_test/controller.py` — add event analysis to Phase 7a after health sweep
- `src/k8s_scale_test/anomaly.py` — minor: log EVENT_SURGE alert type distinctly

### Verification

- Run against existing test data in `scale-test-results/2026-03-18_11-34-04/` to verify that the `Failed` (3,059 events) and `FailedCreatePodSandBox` (1,249 events) reasons would have triggered investigations
- The chart's "Investigated?" column should show "✓ In findings" for all high-volume warning reasons after the fix
- All 244 existing tests must pass
- Test with `--hold-at-peak 0` to verify graceful handling (event analysis should still run, just after the zero-second hold)

## Recent Changes (Previous Session)

1. **Chart: Investigation Summary** — replaced the simple error count table with a comprehensive section showing anomaly findings (deduplicated root causes, evidence chains, investigation layers), proactive scanner results, K8s warning events with investigation status, and diagnostic collection notes.
2. **Anomaly: SSM-verified root causes** — the correlator no longer assumes MAC collision for FailedCreatePodSandBox. It checks SSM logs for the specific signature before labeling.
3. **Anomaly: Post-SSM KB check** — added a second KB lookup after SSM diagnostics are collected, so KB entries with log_patterns can match.
4. **Scanner: Service-level error grouping** — changed the CloudWatch Insights query to group by systemd_unit and count distinct hosts.
5. **Chart: Uninvestigated event flagging** — cross-references warning events against investigation evidence and flags high-volume reasons that were never covered.

## Running Tests

```bash
python3 -m pytest tests/ -v
```

## Key Files

- `src/k8s_scale_test/anomaly.py` — the reactive investigation pipeline
- `src/k8s_scale_test/observability.py` — the proactive scanner
- `src/k8s_scale_test/events.py` — EventWatcher (event streaming, needs warning summary)
- `src/k8s_scale_test/monitor.py` — PodRateMonitor (has the `_alert_in_flight` pattern)
- `src/k8s_scale_test/controller.py` — wires everything together, Phase 7a is the target
- `src/k8s_scale_test/models.py` — Alert, AlertType, Finding data models
- `src/k8s_scale_test/chart.py` — investigation summary chart generation
- `src/k8s_scale_test/reviewer.py` — skeptical finding review
- `src/k8s_scale_test/shared_context.py` — cross-source correlation store
