# Design Document: Cross-Source Correlation via Shared Context

## Overview

This feature adds a lightweight in-memory `SharedContext` that bridges the ObservabilityScanner (proactive) and AnomalyDetector (reactive) modules. The scanner writes `ScanResult` findings to the SharedContext as they're produced. When an alert triggers investigation, the anomaly detector queries the SharedContext for temporally relevant scanner findings before starting its pipeline. If a scanner finding matches (same time window, optionally overlapping affected resources), the anomaly detector references it as prior evidence instead of re-collecting the same data.

### Design Decision: In-Memory vs File-Based

Two approaches were considered:

1. **In-memory data structure with asyncio.Lock** (chosen)
2. **Extend ContextFileWriter to persist scanner findings to agent_context.json**

Option 1 was chosen because:
- ContextFileWriter does synchronous JSON file I/O (read-modify-write) on every call. At 15-30s scan intervals with multiple findings per cycle, this adds unnecessary disk latency to the scanner's hot path.
- The correlation is a real-time, in-process concern — the anomaly detector needs sub-millisecond access to recent scanner findings, not a durable store.
- The ContextFileWriter serves a different purpose (AI sub-agent integration via disk file). Scanner findings are already persisted to `scanner_findings.jsonl` by the evidence store.
- In-memory is simpler: no file locking, no JSON parse/serialize overhead, no partial-write corruption risk.

### Design Decision: asyncio.Lock vs No Lock

Both the scanner and anomaly detector run in the same asyncio event loop (single-threaded). The scanner's `_on_finding` callback is synchronous (not async), so it runs to completion without yielding. This means a synchronous `add()` on the SharedContext cannot interleave with an async `query()` from the anomaly detector at the Python bytecode level.

However, we use an `asyncio.Lock` for the query method anyway because:
- `handle_alert` is async with multiple await points — if we ever make `add()` async in the future, the lock prevents interleaving.
- The lock cost is negligible (uncontended lock acquisition is ~100ns).
- It makes the thread-safety guarantee explicit and self-documenting.

The `add()` method is synchronous (no lock needed) since it's called from a synchronous callback. The `query()` method is async and acquires the lock.

### Design Decision: Correlation Window Default

The default Correlation_Window is 120 seconds. Rationale:
- The scanner runs queries every 15-30 seconds. A 120s window covers 4-8 scan cycles.
- Rate drops typically manifest 30-60 seconds after the underlying issue starts. A 120s window ensures the scanner finding that detected the issue is still available when the alert fires.
- The window is configurable via TestConfig.correlation_window_seconds.

## Architecture

```
ObservabilityScanner                    AnomalyDetector
  │                                       │
  │  _run_query() produces ScanResult     │  handle_alert() receives Alert
  │         │                             │         │
  │         ▼                             │         ▼
  │  _on_finding callback                 │  Query SharedContext (Layer 0)
  │         │                             │         │
  │         ▼                             │    ┌────┴────┐
  │  controller._on_scanner_finding()     │    │ Match?  │
  │         │                             │    └────┬────┘
  │         ├──► evidence_store (existing) │         │
  │         ├──► _scanner_findings (exist) │    yes: add to evidence_refs,
  │         └──► shared_ctx.add() (NEW)   │         feed into _correlate()
  │                                       │    no:  proceed normally
  │                                       │
  └───────── SharedContext ◄──────────────┘
              (in-memory, asyncio.Lock)
              - entries: list[(datetime, ScanResult)]
              - max_age: timedelta
              - add(result): sync, append + evict
              - query(timestamp, window): async, filter by time
```

### Integration Points

The SharedContext is wired by the controller. The scanner does not receive the SharedContext directly — the controller's existing `_on_scanner_finding` callback (which already logs, persists, and collects findings) gains one additional line: `shared_ctx.add(result)`. This keeps the scanner decoupled from the SharedContext.

```
controller.py
  │
  ├── Creates SharedContext(max_age_seconds=event_time_window_minutes * 60)
  │
  ├── _on_scanner_finding callback calls shared_ctx.add(result)
  │
  └── Passes SharedContext to AnomalyDetector(shared_ctx=shared_ctx)
        Anomaly detector queries it in handle_alert()
```

## Components and Interfaces

### New: SharedContext class

Location: `src/k8s_scale_test/shared_context.py`

```python
class SharedContext:
    """In-memory store for cross-module finding correlation.

    Stores timestamped ScanResult entries. The scanner side writes
    via add() (synchronous). The anomaly detector side reads via
    query() (async, acquires lock).
    """

    def __init__(self, max_age_seconds: float = 300.0) -> None:
        self._entries: list[tuple[datetime, ScanResult]] = []
        self._max_age = timedelta(seconds=max_age_seconds)
        self._lock = asyncio.Lock()

    def add(self, result: ScanResult, timestamp: datetime | None = None) -> None:
        """Add a ScanResult entry. Synchronous — safe from sync callbacks.

        Appends the entry and evicts stale entries older than max_age.
        timestamp defaults to datetime.now(timezone.utc) if not provided.
        """

    async def query(
        self, reference_time: datetime, window_seconds: float = 120.0
    ) -> list[tuple[datetime, ScanResult]]:
        """Return entries within ±window_seconds of reference_time.

        Acquires asyncio.Lock. Returns list of (timestamp, ScanResult)
        tuples sorted by timestamp descending (most recent first).
        """

    def _evict(self) -> None:
        """Remove entries older than max_age from the current time."""
```

### New: match_findings function

Location: `src/k8s_scale_test/shared_context.py`

```python
def match_findings(
    scanner_entries: list[tuple[datetime, ScanResult]],
    alert: Alert,
    alert_resources: set[str],
) -> list[tuple[datetime, ScanResult, str]]:
    """Match scanner findings against an alert.

    Returns list of (timestamp, ScanResult, match_type) where
    match_type is "strong" (temporal + resource overlap) or
    "weak" (temporal only).

    Resource extraction: ScanResult stores fleet-aggregated data in
    title and detail strings. Node names matching the pattern
    ip-X-X-X-X.ec2.internal are extracted via regex. If no node
    names are found in a ScanResult, the match is "weak".

    Results are sorted by severity (CRITICAL first) then timestamp
    (most recent first).
    """
```

### Modified: AnomalyDetector.__init__

Add optional `shared_ctx` parameter:

```python
def __init__(
    self, config: TestConfig, k8s_client,
    node_metrics: NodeMetricsAnalyzer,
    node_diag: NodeDiagnosticsCollector,
    evidence_store: EvidenceStore, run_id: str,
    operator_cb=None, aws_client=None,
    kb_store=None, kb_matcher=None,
    amp_collector=None,       # From Feature 2 (AMP reactive investigation)
    shared_ctx=None,          # NEW: Optional[SharedContext]
) -> None:
```

### Modified: AnomalyDetector.handle_alert

Insert SharedContext lookup as "Layer 0" — before K8s events. The actual resource matching happens after Layer 4 (stuck nodes) when we know the affected resources:

```python
async def handle_alert(self, alert: Alert) -> Finding:
    # ... existing setup ...

    # Layer 0: Fetch scanner findings from SharedContext (time-windowed)
    scanner_matches_raw = []
    if self.shared_ctx is not None:
        with span("investigation/shared_context"):
            scanner_matches_raw = await self.shared_ctx.query(
                alert.timestamp,
                window_seconds=self.config.correlation_window_seconds,
            )

    # ... Layer 1: K8s events (existing) ...
    # ... Layer 2: KB lookup (existing, may short-circuit) ...
    # ... Layer 3: Pod phases (existing) ...
    # ... Layer 4: Stuck nodes + conditions (existing) ...

    # Layer 0 continued: Match scanner findings against alert resources
    scanner_matches = []
    if scanner_matches_raw:
        alert_resources = set()
        alert_resources.update(alert.context.get("namespaces", []))
        alert_resources.update(n for n, _ in stuck_nodes)
        alert_resources.update(e.involved_object_name for e in events)
        scanner_matches = match_findings(scanner_matches_raw, alert, alert_resources)
        if scanner_matches:
            log.info("  SharedContext: %d scanner matches (%d strong)",
                     len(scanner_matches),
                     sum(1 for _, _, m in scanner_matches if m == "strong"))

    # ... Layer 4.5: AMP metrics (Feature 2, if configured) ...
    # ... Layer 5: ENI (existing) ...
    # ... Layer 6: SSM (existing) ...

    # Pass scanner_matches to _correlate
    finding = self._correlate(alert, events, metrics, diags,
                              stuck_nodes, eni_evidence, phase_breakdown,
                              amp_metrics=amp_metrics,
                              scanner_matches=scanner_matches)
```

### Modified: AnomalyDetector._correlate

Add scanner match evidence to evidence_references:

```python
def _correlate(self, alert, events, metrics, diagnostics,
               stuck_nodes, eni_evidence, phase_breakdown,
               amp_metrics=None, scanner_matches=None):
    # ... existing evidence collection ...

    # Scanner correlation evidence
    if scanner_matches:
        strong = [m for m in scanner_matches if m[2] == "strong"]
        weak = [m for m in scanner_matches if m[2] == "weak"]
        evidence_refs.append(f"scanner_correlation:{len(strong)}_strong,{len(weak)}_weak")
        for ts, result, match_type in scanner_matches[:5]:
            evidence_refs.append(f"scanner:{result.query_name}({match_type})")
```

### Modified: AnomalyDetector._extract_root_cause

Add scanner-based clues:

```python
def _extract_root_cause(self, warning_reasons, eni_evidence,
                        diagnostics, phase_breakdown,
                        amp_metrics=None, scanner_matches=None):
    # ... existing clue extraction ...

    # Scanner correlation clues (WARNING/CRITICAL findings only)
    if scanner_matches:
        for ts, result, match_type in scanner_matches:
            if result.severity.value in ("warning", "critical"):
                clues.append(f"Scanner pre-detected: {result.title}")
```

### Modified: Controller._on_scanner_finding

Add SharedContext write:

```python
def _on_scanner_finding(self, result: ScanResult) -> None:
    """Handle a scanner finding: log, persist, collect, share."""
    log.warning("Scanner [%s] %s: %s", result.query_name, result.severity.value, result.title)
    try:
        self.evidence_store.save_scanner_finding(self._evidence_run_id, result)
    except Exception as exc:
        log.debug("Failed to persist scanner finding: %s", exc)
    self._scanner_findings.append(result)
    # Write to shared context for cross-source correlation
    if self._shared_ctx is not None:
        try:
            self._shared_ctx.add(result)
        except Exception as exc:
            log.debug("Failed to write scanner finding to shared context: %s", exc)
```

### Modified: Controller.__init__ and run()

```python
# In __init__:
self._shared_ctx: SharedContext | None = None

# In run(), after scanner creation succeeds:
if self._scanner:
    from k8s_scale_test.shared_context import SharedContext
    self._shared_ctx = SharedContext(
        max_age_seconds=self.config.event_time_window_minutes * 60,
    )

# When creating AnomalyDetector:
anomaly = AnomalyDetector(
    self.config, self.k8s_client, node_metrics, node_diag,
    self.evidence_store, run_id, self._prompt_operator,
    aws_client=self.aws_client,
    kb_store=kb_store, kb_matcher=kb_matcher,
    amp_collector=amp_collector,
    shared_ctx=self._shared_ctx,  # NEW
)
```

### New: TestConfig field

```python
correlation_window_seconds: float = 120.0
```

Added to TestConfig with default 120.0. Used by AnomalyDetector when querying SharedContext.

## Data Models

No new dataclasses are needed. The feature reuses:

- **ScanResult** (from `observability.py`): The scanner finding stored in SharedContext. Fields: query_name, severity, title, detail, source, raw_result, drill_down_source, drill_down_query. Note: ScanResult has no timestamp field — the SharedContext records timestamps externally when `add()` is called.
- **Alert** (from `models.py`): The alert that triggers investigation. Fields: alert_type, timestamp, message, context (dict with namespaces, current_rate, etc.).
- **Finding** (from `models.py`): The investigation result. The `evidence_references` list receives scanner correlation strings.

The SharedContext stores entries as `list[tuple[datetime, ScanResult]]`. The `match_findings` function returns `list[tuple[datetime, ScanResult, str]]` where the string is the match type ("strong" or "weak").

### Method Signature Changes

`_correlate` and `_extract_root_cause` gain a `scanner_matches` parameter:

```python
def _correlate(self, alert, events, metrics, diagnostics,
               stuck_nodes, eni_evidence, phase_breakdown,
               amp_metrics=None, scanner_matches=None):  # NEW

def _extract_root_cause(self, warning_reasons, eni_evidence,
                        diagnostics, phase_breakdown,
                        amp_metrics=None, scanner_matches=None):  # NEW
```

Default `None` preserves backward compatibility for existing callers and tests.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Add-then-query round trip

*For any* ScanResult and any timestamp, adding the ScanResult to the SharedContext and then querying with a window that includes that timestamp should return the same ScanResult with all fields intact (query_name, severity, title, detail, source) and the correct associated timestamp.

**Validates: Requirements 1.1, 1.3, 2.2**

### Property 2: Query window filtering

*For any* set of timestamped ScanResult entries in the SharedContext and any reference timestamp and window size, the query method should return exactly those entries whose timestamps fall within ±window_seconds of the reference timestamp, and no entries outside that window.

**Validates: Requirements 1.4, 3.2**

### Property 3: Eviction by max age

*For any* SharedContext with a configured max_age and any set of entries with varying timestamps, after calling add() with a new entry, all entries older than max_age from the current time should be evicted, and all entries within max_age should be retained.

**Validates: Requirements 1.5**

### Property 4: Concurrent read/write safety

*For any* sequence of interleaved add() and query() operations executed as concurrent asyncio coroutines, the SharedContext should not raise exceptions and should not return corrupted data (entries with None timestamps or None ScanResults).

**Validates: Requirements 1.2, 8.3**

### Property 5: Matching correctness — strong vs weak vs excluded

*For any* set of timestamped ScanResult entries and any Alert with known affected resources, match_findings should classify each entry as: "strong" if the entry is within the time window AND has resource overlap with the alert, "weak" if the entry is within the time window but has no resource overlap, or excluded if the entry is outside the time window. No entry outside the time window should appear in the results.

**Validates: Requirements 4.1, 4.2, 4.3**

### Property 6: Matching determinism

*For any* set of scanner entries, alert, and alert resources, calling match_findings twice with the same inputs should produce identical output (same entries, same match types, same order).

**Validates: Requirements 4.4**

### Property 7: Scanner evidence in evidence_references

*For any* non-empty list of scanner matches passed to _correlate, the resulting Finding's evidence_references should contain at least one entry with the "scanner:" prefix and one entry with the "scanner_correlation:" prefix.

**Validates: Requirements 5.1**

### Property 8: Scanner clues in root cause for WARNING/CRITICAL

*For any* list of scanner matches containing at least one entry with severity WARNING or CRITICAL, the _extract_root_cause output should contain at least one "Scanner pre-detected:" clue string that includes the matched finding's title.

**Validates: Requirements 5.2**

### Property 9: Valid Finding regardless of SharedContext state

*For any* Alert and any AnomalyDetector (with SharedContext containing matches, SharedContext containing no matches, SharedContext that raises on query, or SharedContext set to None), handle_alert should produce a valid Finding with non-None finding_id, non-None timestamp, and a valid Severity value.

**Validates: Requirements 7.2, 8.5**

## Error Handling

| Error Scenario | Handling |
|---|---|
| SharedContext.add() raises any exception | Controller's _on_scanner_finding logs at DEBUG level, continues. Scanner is unaffected. |
| SharedContext.query() raises any exception | AnomalyDetector logs at WARNING level, sets scanner_matches to empty list, continues full investigation pipeline. |
| SharedContext is None on AnomalyDetector | SharedContext lookup skipped entirely. No log, no evidence reference. Identical to current behavior. |
| SharedContext is None on Controller | _on_scanner_finding skips the add() call. Scanner operates normally. |
| match_findings receives empty scanner_entries | Returns empty list. No scanner evidence added to Finding. |
| match_findings receives empty alert_resources | All temporal matches become "weak" (no resource overlap possible). |
| ScanResult with no extractable node names in title/detail | match_findings classifies as "weak" match (temporal only). |
| SharedContext memory growth | Bounded by max_age eviction on every add(). At 15-30s scan intervals with ~12 queries per cycle, max entries ≈ (max_age / 15) × 12 ≈ 240 entries for 5-minute max_age. Each ScanResult is ~1KB, so ~240KB max. |

## Testing Strategy

### Test Framework

- **Unit tests**: pytest (existing framework)
- **Property-based tests**: hypothesis (Python PBT library)
- Minimum 100 iterations per property test

### Test File Location

All tests go in `tests/test_shared_context.py`, following the existing pattern of `tests/test_anomaly_proactive.py`.

### Unit Tests

Unit tests cover specific examples and edge cases:

1. **SharedContext.add() with None shared_ctx**: Controller callback completes without error.
2. **SharedContext.query() returns empty for no entries**: Empty SharedContext returns empty list.
3. **AnomalyDetector with None shared_ctx**: handle_alert completes, no scanner evidence in Finding.
4. **Controller wiring with scanner**: SharedContext created, passed to AnomalyDetector.
5. **Controller wiring without scanner**: SharedContext is None, AnomalyDetector receives None.
6. **match_findings with empty inputs**: Returns empty list.
7. **Structural: span name**: handle_alert source contains `span("investigation/shared_context")`.

### Property-Based Tests

Each property test uses hypothesis to generate random inputs:

- **Property 1 test**: Generate random ScanResult objects (random query_name, severity, title, detail, source), add to SharedContext with known timestamps, query back, verify round-trip equality.
  - Tag: **Feature: cross-source-correlation, Property 1: Add-then-query round trip**

- **Property 2 test**: Generate a list of random (timestamp, ScanResult) pairs spanning a wide time range, add all to SharedContext, query with random reference_time and window_seconds, verify returned entries are exactly those within the window.
  - Tag: **Feature: cross-source-correlation, Property 2: Query window filtering**

- **Property 3 test**: Generate entries with timestamps ranging from very old to recent, add to SharedContext with a known max_age, verify old entries are evicted and recent entries are retained.
  - Tag: **Feature: cross-source-correlation, Property 3: Eviction by max age**

- **Property 4 test**: Generate random add/query operation sequences, run them as concurrent asyncio tasks, verify no exceptions and no corrupted data in results.
  - Tag: **Feature: cross-source-correlation, Property 4: Concurrent read/write safety**

- **Property 5 test**: Generate random ScanResult entries with varying titles (some containing node names like `ip-10-0-1-1.ec2.internal`, some not), random Alert with known affected resources, call match_findings, verify strong/weak/excluded classification is correct based on time window and resource overlap.
  - Tag: **Feature: cross-source-correlation, Property 5: Matching correctness — strong vs weak vs excluded**

- **Property 6 test**: Generate random inputs for match_findings, call twice, verify identical output.
  - Tag: **Feature: cross-source-correlation, Property 6: Matching determinism**

- **Property 7 test**: Generate random scanner matches, call _correlate with them, verify evidence_references contains "scanner:" and "scanner_correlation:" prefixed entries.
  - Tag: **Feature: cross-source-correlation, Property 7: Scanner evidence in evidence_references**

- **Property 8 test**: Generate random scanner matches with at least one WARNING or CRITICAL severity, call _extract_root_cause, verify output contains "Scanner pre-detected:" with the finding's title.
  - Tag: **Feature: cross-source-correlation, Property 8: Scanner clues in root cause for WARNING/CRITICAL**

- **Property 9 test**: Generate random Alerts, create AnomalyDetector instances with varying SharedContext states (populated, empty, raising, None), mock the investigation layers, call handle_alert, verify valid Finding produced in all cases.
  - Tag: **Feature: cross-source-correlation, Property 9: Valid Finding regardless of SharedContext state**
