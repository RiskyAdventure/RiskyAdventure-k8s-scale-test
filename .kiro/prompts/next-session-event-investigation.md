# Next Session: Post-Test Event Analysis

Read `.kiro/steering/engineering-rigor.md` before starting any work. Follow it rigorously — form testable hypotheses, negatively test them, read callers/tests/config before changing code, and validate findings before implementing.

## Project Context

This is a Kubernetes scale test tool (`src/k8s_scale_test/`) that scales 30K pods on EKS and monitors the scaling process. It has six monitoring modules that run concurrently during tests:

- `monitor.py` — PodRateMonitor: deployment watch-based pod ready rate tracking
- `anomaly.py` — AnomalyDetector: reactive investigation when rate drops are detected
- `observability.py` — ObservabilityScanner: proactive AMP/CloudWatch scanning
- `health_sweep.py` — HealthSweepAgent: per-node health at hold-at-peak
- `events.py` — EventWatcher: K8s warning event streaming to events.jsonl
- `controller.py` — Observer: independent pod count cross-check

Read `docs/architecture.md`, `docs/anomaly-detection.md`, and `docs/observability-scanner.md` for full design context.

## Problem Statement

The anomaly detector investigates scaling problems — rate drops, pending timeouts. That part works. The gap is everything else.

During a scale test, thousands of K8s warning events accumulate that have nothing to do with scaling speed but indicate real cluster health problems: container runtime failures (`Failed`, `FailedCreatePodContainer`), CNI issues (`FailedCreatePodSandBox`), evictions, OOM kills. These get recorded to `events.jsonl` by the EventWatcher but nobody ever investigates them. They show up on the chart as raw event counts with "⚠ Not investigated" flags, but the analysis pipeline never runs against them.

Evidence from the 2026-03-18 run: 16,999 warning events across 5 reasons. The anomaly detector investigated 1 rate drop and covered `FailedCreatePodSandBox` in its evidence. But `Failed` (3,059 events — runc container creation errors) and `FailedCreatePodContainer` (20 events) were never investigated. These are real problems worth understanding, not scaling artifacts.

## Design: Post-Test Event Analysis Pass

After scaling completes and the hold-at-peak health sweep finishes, run a targeted investigation pass against uninvestigated high-volume warning patterns. This happens in Phase 7a of the controller, during hold-at-peak, when the cluster is stable and SSM calls won't interfere with scaling.

The scaling investigation (rate-drop triggered) stays exactly as-is. This is a separate, additive pass that catches the non-scaling problems the existing pipeline misses.

### What It Does

1. After the health sweep completes during hold-at-peak, query the EventWatcher for accumulated warning reason counts
2. Cross-reference against existing findings to identify reasons that were never covered by a rate-drop investigation
3. For each uncovered reason with significant volume (>100 events), run the anomaly detector's investigation pipeline to produce a proper Finding with root cause, evidence, and SSM diagnostics
4. These findings appear in the chart's Investigation Summary alongside the rate-drop findings

### Implementation Plan

#### 1. EventWatcher: Expose warning summary (`events.py`)

Add a method to query accumulated warning counts and identify uncovered reasons:

```python
def get_warning_summary(self) -> dict[str, dict]:
    """Return {reason: {count, sample_message, kind}} for all warning reasons."""

def get_uncovered_reasons(self, findings: list, threshold: int = 100) -> list[tuple[str, dict]]:
    """Return warning reasons with >threshold events not covered by any finding."""
```

#### 2. New AlertType (`models.py`)

Add `AlertType.EVENT_ANALYSIS` for post-test event-triggered alerts. Context includes the specific warning reason, count, and sample messages.

#### 3. Controller: Post-test event analysis (`controller.py`)

In Phase 7a, after the health sweep, before cleanup:

```python
# After health sweep completes
uncovered = event_watcher.get_uncovered_reasons(self._findings, threshold=100)
for reason, info in uncovered[:3]:  # Cap at 3 to bound hold-at-peak extension
    alert = Alert(
        alert_type=AlertType.EVENT_ANALYSIS,
        timestamp=datetime.now(timezone.utc),
        message=f"Post-test analysis: {reason} x{info['count']}",
        context={"reason": reason, "count": info["count"],
                 "sample": info["sample_message"], "namespaces": namespaces},
    )
    finding = await anomaly.handle_alert(alert)
    self._findings.append(finding)
```

#### 4. AnomalyDetector: No major changes needed (`anomaly.py`)

`handle_alert` already collects K8s events in Layer 1 regardless of alert type. Add a log line to distinguish post-test analysis from rate-drop investigations. The full 6-layer pipeline (events → pod phases → stuck nodes → node conditions → AMP → ENI → SSM) runs as normal.

### Design Constraints

- Cap at 3 event-analysis investigations per run to keep hold-at-peak extension under 5 minutes (each investigation takes ~60-90s with SSM)
- Run sequentially, not concurrently — avoids SSM contention
- Skip reasons already covered by rate-drop findings (dedup via evidence_references)
- If `--hold-at-peak 0`, still run the event analysis after the zero-second hold but before cleanup
- The `_alert_in_flight` mutex in PodRateMonitor is irrelevant here — the monitor is stopped before hold-at-peak event analysis runs

### Key Files to Modify

- `src/k8s_scale_test/events.py` — add `get_warning_summary()` and `get_uncovered_reasons()`
- `src/k8s_scale_test/models.py` — add `AlertType.EVENT_ANALYSIS`
- `src/k8s_scale_test/controller.py` — add event analysis to Phase 7a after health sweep
- `src/k8s_scale_test/anomaly.py` — minor: log EVENT_ANALYSIS alert type distinctly

### Verification

- The chart's "Investigated?" column should show "✓ In findings" for all high-volume warning reasons after the fix
- All 244 existing tests must pass
- Test with `--hold-at-peak 0` to verify event analysis still runs

## Recent Changes (Previous Session)

1. **Chart: Investigation Summary** — replaced the simple error count table with a comprehensive section showing anomaly findings with deduplicated root causes, evidence chains, investigation layers, proactive scanner results, K8s warning events with investigation status, and diagnostic collection notes.
2. **Anomaly: SSM-verified root causes** — the correlator no longer assumes MAC collision for FailedCreatePodSandBox. It checks SSM logs for the specific signature before labeling.
3. **Anomaly: Post-SSM KB check** — added a second KB lookup after SSM diagnostics are collected, so KB entries with log_patterns can match.
4. **Scanner: Service-level error grouping** — changed the CloudWatch Insights query to group by systemd_unit and count distinct hosts.
5. **Chart: Uninvestigated event flagging** — cross-references warning events against investigation evidence and flags high-volume reasons that were never covered.

## Running Tests

```bash
python3 -m pytest tests/ -v
```

## Key Files

- `src/k8s_scale_test/controller.py` — Phase 7a is the target for the new analysis pass
- `src/k8s_scale_test/events.py` — EventWatcher (needs warning summary methods)
- `src/k8s_scale_test/anomaly.py` — the investigation pipeline (runs as-is for event alerts)
- `src/k8s_scale_test/models.py` — Alert, AlertType, Finding data models
- `src/k8s_scale_test/monitor.py` — PodRateMonitor (unchanged, for reference)
- `src/k8s_scale_test/chart.py` — investigation summary chart generation
- `src/k8s_scale_test/observability.py` — the proactive scanner
