# Design Document: Skeptical Review — Independent Finding Re-verification

## Overview

This feature adds a `FindingReviewer` class (`src/k8s_scale_test/reviewer.py`) that independently re-verifies findings produced by the AnomalyDetector. After `anomaly.handle_alert()` returns a Finding, the controller schedules an asynchronous review task that:

1. Identifies the central claim (root_cause or symptom)
2. Re-queries AMP/CloudWatch using a different approach than the original investigation
3. Checks for staleness (time elapsed since finding)
4. Assigns confidence: high / medium / low
5. Lists alternative explanations
6. Generates checkpoint questions if confidence < high
7. Appends a `review` field to the finding JSON and persists it

The review runs as a fire-and-forget `asyncio.create_task` so it never blocks the scaling loop. The controller collects pending review tasks and awaits them during cleanup before generating the summary.

### Design Decision: Re-Query Strategy

The original investigation uses range queries (e.g., `rate(...[5m])`) and multi-layer evidence collection. The reviewer uses a different approach to provide independent validation:

- **AMP**: Uses instant queries at the finding's timestamp instead of range queries. For example, if the investigation found high CPU via `avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))`, the reviewer queries `avg(100 - rate(node_cpu_seconds_total{mode="idle"}[2m]) * 100)` — a different rate window that would show the same signal if the issue is real.
- **CloudWatch**: Queries with a narrower time window centered on the finding timestamp (±1 minute instead of ±5 minutes) to check if the error pattern is concentrated around the finding time.

This approach was chosen over re-running the exact same queries because identical queries would always confirm the original finding — that's not independent verification.

### Design Decision: Async Fire-and-Forget with Cleanup Await

Two approaches were considered for the controller integration:

1. **Inline await after handle_alert** — simpler but blocks the scaling loop for 1-5 seconds per review (AMP + CloudWatch queries)
2. **Fire-and-forget task with cleanup await** (chosen) — review runs in background, controller collects task handles, awaits them all during cleanup

Option 2 was chosen because:
- The scaling loop is time-sensitive — blocking it for re-queries adds latency to rate measurements
- Reviews are not needed in real-time; they're consumed post-run
- The cleanup phase already has a natural synchronization point before summary generation
- Consistent with how CL2 preload runs concurrently (`cl2_task = asyncio.create_task(...)`)

### Design Decision: Reusing Existing Executors

The FindingReviewer receives the same `AMPMetricCollector` and CloudWatch executor function that the ObservabilityScanner uses. This avoids:
- Creating duplicate boto3 sessions and SigV4 signers
- Managing separate credential refresh logic
- Inconsistent AMP endpoint configuration

The controller already constructs these in `_make_prometheus_executor` and `_make_cloudwatch_executor`. The reviewer gets them via constructor injection.

## Architecture

```
Controller.run()
  │
  ├── anomaly.handle_alert(alert) → Finding
  │         │
  │         ▼
  │   reviewer.review(finding)  ← asyncio.create_task (non-blocking)
  │         │
  │         ├── Identify central claim (root_cause or symptom)
  │         ├── Re-query AMP (instant query at finding timestamp)
  │         ├── Re-query CloudWatch (narrow window around finding)
  │         ├── Check staleness (elapsed > threshold?)
  │         ├── Assign confidence (high/medium/low)
  │         ├── Generate alternative explanations
  │         ├── Generate checkpoint questions (if confidence < high)
  │         └── Append review field → persist to EvidenceStore
  │
  ├── ... scaling continues unblocked ...
  │
  └── Cleanup phase:
      ├── await all pending review tasks
      └── generate summary (reviews are now persisted)
```

### Integration with Controller

```python
# In controller.py — after handle_alert returns a finding:

finding = await anomaly.handle_alert(alert)
self._findings.append(finding)

# Schedule async review (non-blocking)
if self._reviewer is not None:
    task = asyncio.create_task(self._safe_review(finding))
    self._review_tasks.append(task)
```

```python
# In cleanup, before summary generation:
if self._review_tasks:
    await asyncio.gather(*self._review_tasks, return_exceptions=True)
```

### Dependency on Features 2 and 3

This feature builds on:
- **Feature 2 (AMP Reactive Investigation)**: Adds `amp_collector` to AnomalyDetector. The reviewer reuses the same `AMPMetricCollector` instance for its re-queries.
- **Feature 3 (Cross-Source Correlation)**: Adds `SharedContext` for scanner↔anomaly correlation. The reviewer does not interact with SharedContext directly — it operates on the already-produced Finding.

The reviewer can function without either feature (graceful degradation to evidence-only review).

## Components and Interfaces

### New: FindingReviewer class

Location: `src/k8s_scale_test/reviewer.py`

```python
class FindingReviewer:
    """Independently re-verifies anomaly findings.

    Takes a Finding, re-queries data sources using a different approach,
    checks for staleness, assigns confidence, and appends a review field.
    """

    def __init__(
        self,
        evidence_store: EvidenceStore,
        run_id: str,
        amp_collector: AMPMetricCollector | None = None,
        cloudwatch_fn: Callable[[str, str, str], Awaitable[dict]] | None = None,
        staleness_threshold_seconds: float = 300.0,
    ) -> None:
        """
        Parameters
        ----------
        evidence_store : EvidenceStore
            For persisting the updated finding with review field.
        run_id : str
            Current test run identifier.
        amp_collector : AMPMetricCollector | None
            For PromQL re-queries. None if AMP not configured.
        cloudwatch_fn : callable | None
            CloudWatch Logs Insights executor. None if CW not configured.
        staleness_threshold_seconds : float
            Seconds after which a finding is considered potentially stale.
        """

    async def review(self, finding: Finding) -> dict:
        """Run the full review pipeline on a finding.

        Returns the review dict (also persisted to EvidenceStore).
        """

    def _identify_claim(self, finding: Finding) -> str:
        """Extract the central claim from a finding.

        Uses root_cause if non-None, otherwise falls back to symptom.
        """

    async def _requery_amp(self, finding: Finding) -> list[dict]:
        """Re-query AMP with different query approach.

        Uses instant queries at the finding timestamp instead of
        the range queries used by the original investigation.

        Returns list of verification_result dicts.
        """

    async def _requery_cloudwatch(self, finding: Finding) -> list[dict]:
        """Re-query CloudWatch with narrower time window.

        Returns list of verification_result dicts.
        """

    def _check_staleness(self, finding: Finding) -> tuple[bool, str]:
        """Check if finding is potentially stale.

        Returns (is_stale, reasoning_text).
        """

    def _assign_confidence(
        self, verification_results: list[dict], is_stale: bool
    ) -> str:
        """Determine confidence level from verification results and staleness.

        Returns "high", "medium", or "low".
        """

    def _generate_alternatives(self, finding: Finding, verification_results: list[dict]) -> list[str]:
        """Generate alternative explanations based on evidence and re-query results."""

    def _generate_checkpoint_questions(
        self, finding: Finding, confidence: str, verification_results: list[dict]
    ) -> list[str]:
        """Generate actionable checkpoint questions when confidence < high."""

    def _build_review(
        self, confidence: str, reasoning: str,
        alternatives: list[str], checkpoints: list[str],
        verification_results: list[dict],
    ) -> dict:
        """Assemble the review dict matching the steering file schema."""

    def _persist_review(self, finding: Finding, review: dict) -> None:
        """Append review field to finding JSON and save to EvidenceStore."""
```

### Modified: ScaleTestController

```python
# New instance variables in __init__:
self._reviewer: FindingReviewer | None = None
self._review_tasks: list[asyncio.Task] = []

# New method:
async def _safe_review(self, finding: Finding) -> None:
    """Run a finding review, catching all exceptions."""
    try:
        await self._reviewer.review(finding)
    except Exception as exc:
        log.warning("Finding review failed for %s: %s", finding.finding_id, exc)
```

### AMP Re-Query Approach

The reviewer constructs different PromQL queries based on the finding's evidence_references:

| Original Evidence | Reviewer Re-Query |
|---|---|
| `amp:N_violations` (CPU) | `avg(100 - rate(node_cpu_seconds_total{mode="idle"}[2m]) * 100)` — different rate window |
| `amp:N_violations` (memory) | `avg(100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))` — instant snapshot |
| `amp:N_violations` (network) | `sum(rate(node_network_receive_errs_total[1m]) + rate(node_network_transmit_errs_total[1m]))` — shorter window |
| `warnings:FailedCreatePodSandBox=N` | `count(kube_pod_status_phase{phase="Pending"})` — check if pods are still pending |
| `eni:checked=N,zero_prefix=M` | `sum(kube_pod_status_phase{phase="Pending"})` — cross-check pending count via Prometheus |

### CloudWatch Re-Query Approach

The reviewer narrows the CloudWatch query window to ±1 minute around the finding timestamp and searches for the specific error patterns mentioned in the finding's evidence_references:

```python
# Example: finding has warnings:FailedCreatePodSandBox=210
query = (
    "fields @timestamp, @message"
    " | filter @message like /FailedCreatePodSandBox/"
    " | stats count() as cnt by bin(1m)"
    " | sort @timestamp desc"
    " | limit 10"
)
```

## Data Models

### Review Schema (from steering file)

The review field appended to finding JSON:

```json
{
  "review": {
    "confidence": "high | medium | low",
    "reasoning": "explanation of confidence assessment",
    "alternative_explanations": ["other possible causes"],
    "checkpoint_questions": ["specific actionable questions"],
    "verification_results": [
      {"claim": "...", "verified": true, "detail": "..."}
    ]
  }
}
```

### Verification Result Structure

Each verification result represents one claim-level check:

```python
{
    "claim": str,       # The claim being verified
    "verified": bool,   # True if re-query confirms the claim
    "detail": str,      # Evidence supporting the verification decision
}
```

### Confidence Assignment Logic

```
all verified + not stale → "high"
all verified + stale     → "medium" (staleness downgrades by one step)
mixed results            → "medium"
mixed results + stale    → "low"
contradicted / gaps      → "low"
all re-queries failed    → "low"
```

### No New Dataclasses

The review is a plain dict (not a dataclass) because:
- The Finding dataclass in `models.py` doesn't have a `review` field — adding one would change the serialization for all existing findings
- The review is appended at the JSON level after serialization via `finding.to_dict()`
- The schema is defined in the steering file and consumed by downstream tooling as raw JSON
- This matches the pattern used by `agent_context.json` — plain dicts, not dataclasses


## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Central claim identification

*For any* Finding, the identified central claim should equal the Finding's root_cause when root_cause is non-None, and should equal the Finding's symptom when root_cause is None.

**Validates: Requirements 2.1, 2.2**

### Property 2: Valid review without external data sources

*For any* Finding and a FindingReviewer instantiated with amp_collector=None and cloudwatch_fn=None, calling review() should produce a dict containing exactly the keys: "confidence" (one of "high", "medium", "low"), "reasoning" (string), "alternative_explanations" (list of strings), "checkpoint_questions" (list of strings), and "verification_results" (list of dicts each with "claim", "verified", and "detail" keys).

**Validates: Requirements 1.3, 3.3, 8.1**

### Property 3: Re-query execution when sources available

*For any* Finding whose evidence_references contain AMP-related entries and a FindingReviewer with a non-None amp_collector, the amp_collector's query method should be invoked at least once during review. Similarly, for any Finding whose evidence_references contain CloudWatch-related entries and a FindingReviewer with a non-None cloudwatch_fn, the cloudwatch_fn should be invoked at least once.

**Validates: Requirements 3.1, 3.2**

### Property 4: Staleness detection and confidence downgrade

*For any* Finding whose timestamp is more than staleness_threshold_seconds in the past, the review's reasoning field should contain a staleness indication, and the confidence level should be one step lower than it would be without staleness (high→medium, medium→low, low stays low).

**Validates: Requirements 4.1, 4.2, 4.3**

### Property 5: Confidence assignment correctness

*For any* list of verification_results and staleness flag: when all results have verified=True and is_stale=False, confidence should be "high"; when results are mixed (some True, some False) and is_stale=False, confidence should be "medium"; when all results have verified=False or significant gaps exist, confidence should be "low". Staleness downgrades by one step.

**Validates: Requirements 5.1, 5.2, 5.3, 5.4**

### Property 6: Checkpoint questions conditional on confidence

*For any* review with confidence "medium" or "low", the checkpoint_questions list should be non-empty (at least one question). For any review with confidence "high", the checkpoint_questions list should be empty.

**Validates: Requirements 7.1, 7.3**

### Property 7: Review persistence round-trip

*For any* Finding, after review() completes, reading the finding JSON back from the EvidenceStore should yield a dict containing a "review" key whose value matches the review dict returned by review().

**Validates: Requirements 8.2, 8.3**

### Property 8: Graceful degradation on query failure

*For any* Finding and any combination of AMP/CloudWatch query failures (exceptions raised by amp_collector or cloudwatch_fn), the review() method should still return a valid review dict with all required schema fields. When all re-queries fail, confidence should be "low".

**Validates: Requirements 10.1, 10.2, 10.3**

### Property 9: Controller exception isolation

*For any* exception raised by FindingReviewer.review(), the controller's _safe_review wrapper should catch the exception without re-raising, ensuring the scaling loop continues uninterrupted.

**Validates: Requirements 9.2**

## Error Handling

| Error Scenario | Handling |
|---|---|
| AMPMetricCollector._query_promql() raises any exception | Log at WARNING, record failure in Verification_Result (verified=False, detail includes error), continue with remaining checks |
| CloudWatch executor raises any exception | Log at WARNING, record failure in Verification_Result (verified=False, detail includes error), continue with remaining checks |
| All re-queries fail | Produce review based on existing evidence only, set confidence to "low", note in reasoning |
| AMPMetricCollector is None | Skip AMP re-queries entirely, no error logged |
| CloudWatch executor is None | Skip CloudWatch re-queries entirely, no error logged |
| Finding has no evidence_references | Produce review with central claim verification only, no re-queries attempted |
| EvidenceStore._write_json fails during persist | Log at ERROR, review dict is still returned to caller (in-memory result not lost) |
| FindingReviewer.review() raises any exception | Controller's _safe_review catches it, logs at WARNING, scaling loop continues |
| Finding.to_dict() raises during serialization | Caught in _persist_review, logged, review still returned |

The error handling follows the same pattern as the existing anomaly detector: catch broadly, log, continue. The review process should never cause the test run to fail.

## Testing Strategy

### Test Framework

- **Unit tests**: pytest (existing framework)
- **Property-based tests**: hypothesis (Python PBT library)
- Minimum 100 iterations per property test
- Each property test must reference its design document property
- Tag format: **Feature: skeptical-review, Property {number}: {property_text}**

### Unit Tests

Unit tests cover specific examples, edge cases, and integration points:

1. **Constructor stores parameters**: Verify evidence_store, run_id, amp_collector, cloudwatch_fn, staleness_threshold are stored correctly.
2. **Review with KB-matched finding**: Finding with `kb_match:ipamd-mac-collision` evidence reference produces alternatives from KB entry.
3. **Controller wiring**: `inspect.getsource()` structural test verifying the controller creates FindingReviewer and schedules review tasks.
4. **Controller awaits reviews before summary**: `inspect.getsource()` structural test verifying `asyncio.gather(*self._review_tasks` appears before `_make_summary`.
5. **Controller passes same executor instances**: `inspect.getsource()` structural test verifying the reviewer receives the same prom_fn/cw_fn.

### Property-Based Tests

Each correctness property is implemented as a single property-based test:

- **Property 1 test**: Generate random Finding objects with random root_cause (str or None) and random symptom strings. Call _identify_claim, verify result matches root_cause when non-None, symptom otherwise.
  - Tag: **Feature: skeptical-review, Property 1: Central claim identification**

- **Property 2 test**: Generate random Finding objects with varying evidence_references, K8s events, and root causes. Create FindingReviewer with amp_collector=None and cloudwatch_fn=None. Call review(), verify returned dict has all required schema keys with correct types.
  - Tag: **Feature: skeptical-review, Property 2: Valid review without external data sources**

- **Property 3 test**: Generate random Finding objects with AMP/CW evidence references. Create FindingReviewer with mock amp_collector and mock cloudwatch_fn. Call review(), verify mocks were called at least once.
  - Tag: **Feature: skeptical-review, Property 3: Re-query execution when sources available**

- **Property 4 test**: Generate random Finding objects with timestamps ranging from recent to old. Create FindingReviewer with known staleness_threshold. Call review(), verify staleness detection and confidence downgrade for old findings.
  - Tag: **Feature: skeptical-review, Property 4: Staleness detection and confidence downgrade**

- **Property 5 test**: Generate random lists of verification_result dicts (varying verified=True/False) and random is_stale booleans. Call _assign_confidence, verify the result matches the expected confidence level per the rules.
  - Tag: **Feature: skeptical-review, Property 5: Confidence assignment correctness**

- **Property 6 test**: Generate random Finding objects, run review(), check that checkpoint_questions is non-empty when confidence is "medium" or "low", and empty when confidence is "high".
  - Tag: **Feature: skeptical-review, Property 6: Checkpoint questions conditional on confidence**

- **Property 7 test**: Generate random Finding objects, create FindingReviewer with a real EvidenceStore (tmp_path), call review(), read back the finding JSON from disk, verify it contains a "review" key matching the returned dict.
  - Tag: **Feature: skeptical-review, Property 7: Review persistence round-trip**

- **Property 8 test**: Generate random Finding objects with AMP/CW evidence. Create FindingReviewer with mock amp_collector and cloudwatch_fn that raise random exceptions. Call review(), verify a valid review dict is returned with confidence "low" when all fail.
  - Tag: **Feature: skeptical-review, Property 8: Graceful degradation on query failure**

- **Property 9 test**: Generate random exception types. Mock FindingReviewer.review to raise them. Call controller's _safe_review, verify no exception propagates.
  - Tag: **Feature: skeptical-review, Property 9: Controller exception isolation**

### Test File Location

All tests go in `tests/test_reviewer.py`, following the existing pattern of `tests/test_controller_tracing.py` and `tests/test_anomaly_proactive.py`.
