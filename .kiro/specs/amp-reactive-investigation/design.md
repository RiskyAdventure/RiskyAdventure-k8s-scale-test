# Design Document: AMP Reactive Investigation

## Overview

This feature adds an AMP/Prometheus metric query layer to the AnomalyDetector's reactive investigation pipeline. When an alert triggers investigation and AMP is configured, the detector will query node-level metrics (CPU, memory, network errors, IPAMD metrics, pod restarts) scoped to a ±3 minute window around the alert timestamp. Results are integrated into the Finding's evidence_references and fed into root cause extraction.

The design reuses the existing `AMPMetricCollector` from `health_sweep.py` — it already handles SigV4 signing, credential refresh, and concurrent PromQL execution. The controller constructs the collector and passes it to the AnomalyDetector, following the same dependency injection pattern used for `aws_client`, `kb_store`, and `kb_matcher`.

### Design Decision: Instance Injection vs Raw Config

Two approaches were considered for giving AnomalyDetector access to AMP:

1. **Pass a pre-configured AMPMetricCollector instance** (chosen)
2. **Pass `amp_workspace_id` and `aws_profile` strings, construct internally**

Option 1 was chosen because:
- The controller already constructs AMPMetricCollector for the scanner (`_make_prometheus_executor`). Creating a second instance for the anomaly detector follows the same pattern.
- Dependency injection makes testing straightforward — tests pass a mock collector.
- AnomalyDetector doesn't need to know about SigV4, credential refresh, or boto3 sessions.
- Consistent with how `aws_client`, `kb_store`, and `kb_matcher` are already injected.

### Design Decision: Query Timing in Pipeline

The AMP query layer runs after K8s events (Layer 1) and KB lookup (Layer 2), but before the ENI and SSM layers (Layers 5-6). Rationale:
- AMP queries are cheaper than SSM (~100ms HTTP vs ~5s per SSM call) but more expensive than K8s API calls.
- AMP data can inform which nodes to investigate with SSM (e.g., nodes with high CPU pressure).
- KB short-circuit still works — if KB matches, AMP queries are skipped along with everything else.
- AMP results feed into `_correlate()` and `_extract_root_cause()` alongside existing evidence.

## Architecture

```
Alert received
  │
  ▼
Layer 1: K8s Events (existing)
  │
  ▼
Layer 2: KB Lookup (existing, may short-circuit)
  │
  ▼
Layer 3: Pod Phase Breakdown (existing)
  │
  ▼
Layer 4: Stuck Pod Nodes + Node Conditions (existing)
  │
  ▼
Layer 4.5: AMP Metric Query (NEW)  ◄── This feature
  │  Query: CPU, memory, network errors, IPAMD, pod restarts
  │  Scope: ±3 min around alert timestamp
  │  Runs concurrently via AMPMetricCollector.collect_all()
  │
  ▼
Layer 5: EC2 ENI State (existing)
  │
  ▼
Layer 6: SSM Diagnostics (existing)
  │
  ▼
Correlate (existing, enhanced with AMP evidence)
```

### Controller Wiring

```
controller.py
  │
  ├── _make_prometheus_executor(config)  ← existing, for ObservabilityScanner
  │     Creates AMPMetricCollector, wraps in async executor
  │
  ├── Creates AMPMetricCollector for AnomalyDetector  ← NEW
  │     Same construction pattern, passed as amp_collector param
  │
  └── AnomalyDetector(config, ..., amp_collector=collector)
```

## Components and Interfaces

### Modified: AnomalyDetector.__init__

Add optional `amp_collector` parameter:

```python
def __init__(
    self, config: TestConfig, k8s_client,
    node_metrics: NodeMetricsAnalyzer,
    node_diag: NodeDiagnosticsCollector,
    evidence_store: EvidenceStore, run_id: str,
    operator_cb=None, aws_client=None,
    kb_store=None, kb_matcher=None,
    amp_collector=None,  # NEW: Optional[AMPMetricCollector]
) -> None:
```

### New: AnomalyDetector._collect_amp_metrics

```python
async def _collect_amp_metrics(self, alert_timestamp: datetime) -> dict[str, list[NodeMetricResult]]:
    """Query AMP for node-level metrics around the alert timestamp (±3 min).

    Returns dict keyed by metric category (cpu, memory, network_errors,
    ipamd, pod_restarts). Returns empty dict if amp_collector is None
    or all queries fail.
    """
```

This method:
1. Calls `self.amp_collector.collect_all()` to run all PromQL queries concurrently.
2. Returns the results dict keyed by category.
3. Catches all exceptions, logs them, returns empty dict on failure.

Note: The ±3 min time scoping is handled by using PromQL range queries with appropriate time windows rather than instant queries. The existing `AMPMetricCollector._QUERIES` use rate windows (e.g., `rate(...[5m])`) which already cover the relevant time period. For the reactive investigation, we reuse these same queries since the alert timestamp is always recent (within the last few seconds of the current time).

### Modified: AnomalyDetector.handle_alert

Insert AMP query step after Layer 4 (stuck nodes + conditions) and before Layer 5 (ENI):

```python
# Layer 4.5: AMP metrics (if configured)
amp_metrics: dict[str, list[NodeMetricResult]] = {}
if self.amp_collector is not None:
    with span("investigation/amp_metrics"):
        amp_metrics = await self._collect_amp_metrics(alert.timestamp)
```

### Modified: AnomalyDetector._correlate

Add AMP evidence to evidence_references:

```python
# AMP metric evidence
if amp_metrics:
    amp_issues = []
    for category, results in amp_metrics.items():
        for r in results:
            issue = check_threshold(r)
            if issue:
                amp_issues.append(issue)
    if amp_issues:
        evidence_refs.append(f"amp:{len(amp_issues)}_violations")
    else:
        evidence_refs.append(f"amp:checked={sum(len(v) for v in amp_metrics.values())},no_violations")
elif self.amp_collector is not None:
    evidence_refs.append("amp:no_data")
```

### Modified: AnomalyDetector._extract_root_cause

Add AMP-based root cause clues after existing SSM log evidence:

```python
# AMP metric evidence
for category, results in amp_metrics.items():
    for r in results:
        if category == "cpu" and r.value > 90:
            clues.append(f"AMP: CPU {r.value:.1f}% on {r.node_name}")
        elif category == "memory" and r.value > 90:
            clues.append(f"AMP: Memory {r.value:.1f}% on {r.node_name}")
        elif category == "network_errors" and r.value > 0:
            clues.append(f"AMP: Network errors {r.value:.1f}/s on {r.node_name}")
        elif category == "pod_restarts" and r.value > 5:
            clues.append(f"AMP: {r.value:.0f} pod restarts on {r.node_name}")
```

### Modified: Controller (AnomalyDetector construction)

```python
# Create AMP collector for anomaly detector (if configured)
amp_collector = None
if self.config.amp_workspace_id or self.config.prometheus_url:
    try:
        amp_collector = AMPMetricCollector(
            self.config.amp_workspace_id,
            self.config.prometheus_url,
            self.config.aws_profile,
        )
    except Exception as exc:
        log.warning("AMP collector init failed for anomaly detector: %s", exc)

anomaly = AnomalyDetector(
    self.config, self.k8s_client, node_metrics, node_diag,
    self.evidence_store, run_id, self._prompt_operator,
    aws_client=self.aws_client,
    kb_store=kb_store, kb_matcher=kb_matcher,
    amp_collector=amp_collector,
)
```

## Data Models

No new data models are needed. The feature reuses:

- **`NodeMetricResult`** (from `health_sweep.py`): Holds per-node metric values with `node_name`, `metric_category`, and `value`.
- **`Finding`** (from `models.py`): The `evidence_references` list receives AMP summary strings. The `node_metrics` field is not used for AMP data (it holds `NodeMetric` objects from the condition checker, which is a different data shape).
- **`check_threshold()`** (from `health_sweep.py`): Reused to evaluate AMP metric results against the same thresholds used by the health sweep.

### Method Signature Changes

`_correlate` and `_extract_root_cause` gain an `amp_metrics` parameter:

```python
def _correlate(self, alert, events, metrics, diagnostics,
               stuck_nodes, eni_evidence, phase_breakdown,
               amp_metrics=None):  # NEW parameter

def _extract_root_cause(self, warning_reasons, eni_evidence,
                        diagnostics, phase_breakdown,
                        amp_metrics=None):  # NEW parameter
```

Default `None` preserves backward compatibility for any callers or tests that don't pass AMP data.



## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: AMP queries attempted when collector is configured

*For any* Alert and any AnomalyDetector with a non-None amp_collector, calling handle_alert should invoke the collector's collect_all method exactly once.

**Validates: Requirements 2.1**

### Property 2: AMP evidence in evidence_references reflects metric content

*For any* set of AMP metric results (dict of category → list of NodeMetricResult), the resulting Finding's evidence_references should contain an AMP summary entry. When threshold violations exist (CPU > 90%, memory > 90%, network_errors > 0, pod_restarts > 5), the summary should indicate the violation count. When no violations exist, the summary should indicate the number of nodes checked with no violations.

**Validates: Requirements 3.1, 3.2**

### Property 3: AMP threshold violations appear in root cause

*For any* set of AMP metric results containing at least one threshold violation, the _extract_root_cause output should contain an "AMP:" prefixed clue string referencing the node name and metric value for each violation.

**Validates: Requirements 3.3, 3.4**

### Property 4: Graceful degradation on AMP failure

*For any* exception raised by the AMPMetricCollector during collect_all, the handle_alert method should still produce a valid Finding (non-None, with a finding_id, timestamp, and severity) and the investigation pipeline should complete without raising.

**Validates: Requirements 4.1**

## Error Handling

| Error Scenario | Handling |
|---|---|
| AMPMetricCollector.collect_all() raises any exception | Log at WARNING level, set amp_metrics to empty dict, continue pipeline |
| AMPMetricCollector is None | Skip AMP layer entirely (no log, no evidence reference) |
| All AMP queries return empty results | Add "amp:no_data" evidence reference, continue pipeline |
| Individual PromQL query fails within collect_all | AMPMetricCollector already handles this internally — logs warning, returns empty list for that category |
| SigV4 credential expiry during query | AMPMetricCollector already retries once with fresh credentials on HTTP 403 |
| AMPMetricCollector constructor fails in controller | Log warning, pass None to AnomalyDetector — graceful fallback to no-AMP behavior |

The error handling follows the same pattern as the existing ENI and SSM layers: catch broadly, log, continue. The investigation pipeline should never fail because of an optional evidence source.

## Testing Strategy

### Test Framework

- **Unit tests**: pytest (existing framework)
- **Property-based tests**: hypothesis (Python PBT library)
- Minimum 100 iterations per property test

### Unit Tests

Unit tests cover specific examples and edge cases:

1. **Constructor stores collector**: Verify `amp_collector` attribute is set when passed, None when not passed.
2. **handle_alert with None collector**: Full pipeline completes, no AMP calls, valid Finding produced.
3. **handle_alert with configured collector**: collect_all called, AMP evidence in Finding.
4. **Empty AMP results**: "amp:no_data" or similar in evidence_references.
5. **Controller wiring with amp_workspace_id**: AMPMetricCollector created and passed.
6. **Controller wiring without amp config**: None passed for amp_collector.
7. **Structural: span name**: handle_alert source contains `span("investigation/amp_metrics")`.

### Property-Based Tests

Each property test uses hypothesis to generate random inputs:

- **Property 1 test**: Generate random Alert objects, mock collector, verify collect_all called.
  - Tag: **Feature: amp-reactive-investigation, Property 1: AMP queries attempted when collector is configured**
- **Property 2 test**: Generate random dicts of NodeMetricResult lists (varying categories, values, node names), call _correlate, verify evidence_references contain appropriate AMP summary.
  - Tag: **Feature: amp-reactive-investigation, Property 2: AMP evidence in evidence_references reflects metric content**
- **Property 3 test**: Generate random NodeMetricResult lists with at least one value above threshold, call _extract_root_cause, verify output contains "AMP:" clues with correct node names.
  - Tag: **Feature: amp-reactive-investigation, Property 3: AMP threshold violations appear in root cause**
- **Property 4 test**: Generate random exception types, mock collect_all to raise them, run handle_alert, verify valid Finding returned.
  - Tag: **Feature: amp-reactive-investigation, Property 4: Graceful degradation on AMP failure**

### Test File Location

All tests go in `tests/test_anomaly_amp.py`, following the existing pattern of `tests/test_anomaly_proactive.py` and `tests/test_anomaly_tracing.py`.
