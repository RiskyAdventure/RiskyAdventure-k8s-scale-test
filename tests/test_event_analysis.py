"""Property-based tests for EventWatcher event analysis methods.

Feature: post-test-event-investigation
Property 1: Warning summary correctly aggregates events by reason
Property 2: Uncovered reasons filtering is correct and complete

**Validates: Requirements 1.1**
**Validates: Requirements 1.2**
"""

import tempfile
from collections import Counter
from datetime import datetime, timezone
from unittest.mock import MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

from k8s_scale_test.evidence import EvidenceStore
from k8s_scale_test.events import EventWatcher
from k8s_scale_test.models import Finding, K8sEvent, Severity, TestConfig


# --- Strategies ---

# Constrain to printable ASCII-ish identifiers to avoid JSON edge cases
_reason = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=40,
)

_message = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "Z"), whitelist_characters="-_.:/ "),
    min_size=1,
    max_size=120,
)

_kind = st.sampled_from(["Pod", "Node", "Deployment", "ReplicaSet", "Service"])

_event_type = st.sampled_from(["Warning", "Normal"])

_timestamp = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 12, 31),
    timezones=st.just(timezone.utc),
)

_namespace = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-"),
    min_size=1,
    max_size=20,
)


def _k8s_event(
    reason=_reason,
    message=_message,
    kind=_kind,
    event_type=_event_type,
) -> st.SearchStrategy[K8sEvent]:
    """Generate a random K8sEvent with controllable fields."""
    return st.builds(
        K8sEvent,
        timestamp=_timestamp,
        namespace=_namespace,
        involved_object_kind=kind,
        involved_object_name=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-"),
            min_size=1,
            max_size=30,
        ),
        reason=reason,
        message=message,
        event_type=event_type,
        scaling_step=st.integers(min_value=0, max_value=100),
        count=st.integers(min_value=1, max_value=1000),
    )


def _make_watcher(tmpdir: str) -> tuple[EventWatcher, EvidenceStore, str]:
    """Create an EventWatcher backed by a real EvidenceStore in a temp dir."""
    store = EvidenceStore(tmpdir)
    config = TestConfig(target_pods=100)
    run_id = store.create_run(config)

    mock_client = MagicMock()
    watcher = EventWatcher(
        k8s_client=mock_client,
        namespaces=["default"],
        evidence_store=store,
    )
    watcher._run_id = run_id
    return watcher, store, run_id


# --- Property 1: Warning summary correctly aggregates events by reason ---


class TestProperty1WarningSummaryAggregation:
    """Property 1: Warning summary correctly aggregates events by reason.

    For any set of K8sEvent objects written to events.jsonl, calling
    get_warning_summary() should return a dictionary where:
    (a) every key is a reason that appeared in at least one Warning event,
    (b) the count for each reason equals the number of Warning events with that reason,
    (c) the sample_message matches the message of one of the Warning events for that reason,
    (d) the kind matches the involved_object_kind of one of the Warning events for that reason,
    (e) Normal events are excluded from the summary.

    **Validates: Requirements 1.1**
    """

    @given(events=st.lists(_k8s_event(), min_size=0, max_size=50))
    @settings(max_examples=100)
    def test_warning_summary_aggregation(self, events: list[K8sEvent]):
        """Warning summary correctly aggregates events by reason."""
        with tempfile.TemporaryDirectory() as tmpdir:
            watcher, store, run_id = _make_watcher(tmpdir)

            # Write all events to events.jsonl via the real EvidenceStore
            for ev in events:
                store.append_event(run_id, ev)

            summary = watcher.get_warning_summary()

            # Compute expected: only Warning events, grouped by reason
            warning_events = [e for e in events if e.event_type == "Warning"]
            expected_reasons = set(e.reason for e in warning_events)

            # (a) Every key is a reason from a Warning event
            assert set(summary.keys()) == expected_reasons

            # (b) Count for each reason equals number of Warning events with that reason
            reason_counts = Counter(e.reason for e in warning_events)
            for reason, info in summary.items():
                assert info["count"] == reason_counts[reason], (
                    f"Count mismatch for reason={reason!r}: "
                    f"got {info['count']}, expected {reason_counts[reason]}"
                )

            # (c) sample_message is from one of the Warning events for that reason
            for reason, info in summary.items():
                reason_messages = {
                    e.message for e in warning_events if e.reason == reason
                }
                assert info["sample_message"] in reason_messages, (
                    f"sample_message {info['sample_message']!r} not found in "
                    f"Warning events for reason={reason!r}"
                )

            # (d) kind is from one of the Warning events for that reason
            for reason, info in summary.items():
                reason_kinds = {
                    e.involved_object_kind
                    for e in warning_events
                    if e.reason == reason
                }
                assert info["kind"] in reason_kinds, (
                    f"kind {info['kind']!r} not found in "
                    f"Warning events for reason={reason!r}"
                )

            # (e) Normal events are excluded — no Normal-only reason appears
            normal_only_reasons = (
                set(e.reason for e in events if e.event_type == "Normal")
                - expected_reasons
            )
            for reason in normal_only_reasons:
                assert reason not in summary, (
                    f"Normal-only reason {reason!r} should not appear in summary"
                )

    @given(events=st.lists(
        _k8s_event(event_type=st.just("Normal")),
        min_size=0,
        max_size=20,
    ))
    @settings(max_examples=50)
    def test_normal_only_events_produce_empty_summary(self, events: list[K8sEvent]):
        """When all events are Normal, summary should be empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            watcher, store, run_id = _make_watcher(tmpdir)

            for ev in events:
                store.append_event(run_id, ev)

            summary = watcher.get_warning_summary()
            assert summary == {}

    def test_no_events_returns_empty_dict(self):
        """When no events exist, summary should be empty dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            watcher, _, _ = _make_watcher(tmpdir)
            summary = watcher.get_warning_summary()
            assert summary == {}


# --- Strategies for Property 2 ---

# A small pool of reason strings so events and findings share reasons often
_reason_pool = st.sampled_from([
    "Failed", "FailedCreatePodSandBox", "FailedCreatePodContainer",
    "BackOff", "Unhealthy", "Evicted", "OOMKilling",
    "FailedScheduling", "NodeNotReady", "NetworkUnavailable",
])


def _evidence_ref_from_reasons(
    reasons: list[str],
) -> st.SearchStrategy[str]:
    """Build a 'warnings:Reason=N,...' evidence_references string.

    Picks a non-empty subset of the provided reasons and assigns random counts.
    """
    if not reasons:
        return st.just("pods:some-pod")

    return st.lists(
        st.tuples(
            st.sampled_from(reasons),
            st.integers(min_value=1, max_value=9999),
        ),
        min_size=1,
        max_size=min(len(reasons), 5),
    ).map(
        lambda pairs: "warnings:" + ",".join(
            f"{r}={c}" for r, c in pairs
        )
    )


def _k8s_event_with_reason(reason: str) -> st.SearchStrategy[K8sEvent]:
    """Generate a Warning K8sEvent with a specific reason string."""
    return _k8s_event(reason=st.just(reason), event_type=st.just("Warning"))


@st.composite
def _uncovered_scenario(draw):
    """Generate a complete test scenario for get_uncovered_reasons.

    Produces:
    - events: list of K8sEvent (Warning + Normal) to write to events.jsonl
    - findings: list of Finding with realistic evidence_references and k8s_events
    - threshold: int (small, to make the property interesting with small event lists)
    """
    # Draw a set of reason strings from the pool
    all_reasons = draw(st.lists(
        _reason_pool, min_size=1, max_size=8, unique=True,
    ))

    # For each reason, generate 1..20 Warning events so counts vary
    events: list[K8sEvent] = []
    for reason in all_reasons:
        n = draw(st.integers(min_value=1, max_value=20))
        for _ in range(n):
            events.append(draw(_k8s_event_with_reason(reason)))

    # Optionally add some Normal events (should be ignored)
    normal_count = draw(st.integers(min_value=0, max_value=5))
    for _ in range(normal_count):
        events.append(draw(_k8s_event(event_type=st.just("Normal"))))

    # Pick a subset of reasons to be "covered" by findings
    covered_reasons = draw(st.lists(
        st.sampled_from(all_reasons),
        min_size=0,
        max_size=len(all_reasons),
        unique=True,
    ))

    # Decide how each covered reason is covered: via evidence_references or k8s_events
    findings: list[Finding] = []
    # Split covered reasons into two groups
    via_evidence = []
    via_k8s_events = []
    for reason in covered_reasons:
        method = draw(st.sampled_from(["evidence", "k8s_events", "both"]))
        if method in ("evidence", "both"):
            via_evidence.append(reason)
        if method in ("k8s_events", "both"):
            via_k8s_events.append(reason)

    # Build findings that cover reasons via evidence_references
    if via_evidence:
        # Group into 1-3 evidence_references strings
        n_refs = draw(st.integers(min_value=1, max_value=min(3, len(via_evidence))))
        chunks = [via_evidence[i::n_refs] for i in range(n_refs)]
        ev_refs = []
        for chunk in chunks:
            pairs = ",".join(
                f"{r}={draw(st.integers(min_value=1, max_value=9999))}"
                for r in chunk
            )
            ev_refs.append(f"warnings:{pairs}")
        # Add some non-warnings refs for realism
        ev_refs.append("pods:some-pod-xyz")

        findings.append(Finding(
            finding_id=draw(st.text(
                alphabet=st.characters(
                    whitelist_categories=("L", "N"), whitelist_characters="-_",
                ),
                min_size=1, max_size=20,
            )),
            timestamp=draw(_timestamp),
            severity=draw(st.sampled_from(list(Severity))),
            symptom="test symptom",
            affected_resources=[],
            k8s_events=[],
            node_metrics=[],
            node_diagnostics=[],
            evidence_references=ev_refs,
        ))

    # Build findings that cover reasons via k8s_events
    if via_k8s_events:
        k8s_evs = []
        for reason in via_k8s_events:
            k8s_evs.append(K8sEvent(
                timestamp=draw(_timestamp),
                namespace="default",
                involved_object_kind="Pod",
                involved_object_name="test-pod",
                reason=reason,
                message="test message",
                event_type="Warning",
                scaling_step=0,
                count=1,
            ))

        findings.append(Finding(
            finding_id=draw(st.text(
                alphabet=st.characters(
                    whitelist_categories=("L", "N"), whitelist_characters="-_",
                ),
                min_size=1, max_size=20,
            )),
            timestamp=draw(_timestamp),
            severity=draw(st.sampled_from(list(Severity))),
            symptom="test symptom k8s",
            affected_resources=[],
            k8s_events=k8s_evs,
            node_metrics=[],
            node_diagnostics=[],
            evidence_references=[],
        ))

    # Optionally add an empty finding (no coverage)
    if draw(st.booleans()):
        findings.append(Finding(
            finding_id="empty-finding",
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            severity=Severity.INFO,
            symptom="no coverage",
            affected_resources=[],
            k8s_events=[],
            node_metrics=[],
            node_diagnostics=[],
            evidence_references=[],
        ))

    # Threshold: small enough to make the property interesting
    threshold = draw(st.integers(min_value=0, max_value=15))

    return events, findings, threshold, all_reasons, set(covered_reasons)


# --- Property 2: Uncovered reasons filtering is correct and complete ---


class TestProperty2UncoveredReasonsFiltering:
    """Property 2: Uncovered reasons filtering is correct and complete.

    For any set of K8sEvent objects and any list of Finding objects (with
    arbitrary evidence_references and k8s_events), calling
    get_uncovered_reasons(findings, threshold) should return a list where:
    (a) every returned reason has a warning event count strictly > threshold,
    (b) no returned reason appears in any finding's evidence_references
        (as a "warnings:Reason=N" pattern) or in any finding's k8s_events
        reason field,
    (c) every warning reason NOT in the returned list either has
        count <= threshold or IS covered by at least one finding,
    (d) the returned list is sorted by count descending.

    **Validates: Requirements 1.2**
    """

    @given(scenario=_uncovered_scenario())
    @settings(max_examples=100)
    def test_uncovered_reasons_filtering(self, scenario):
        """Uncovered reasons filtering is correct and complete."""
        events, findings, threshold, all_reasons, covered_reasons_set = scenario

        with tempfile.TemporaryDirectory() as tmpdir:
            watcher, store, run_id = _make_watcher(tmpdir)

            for ev in events:
                store.append_event(run_id, ev)

            result = watcher.get_uncovered_reasons(findings, threshold)
            returned_reasons = [r for r, _ in result]

            # Compute the warning summary independently for verification
            warning_events = [e for e in events if e.event_type == "Warning"]
            reason_counts = Counter(e.reason for e in warning_events)

            # Compute covered reasons the same way the implementation does
            actual_covered: set[str] = set()
            for f in findings:
                for ev_ref in f.evidence_references:
                    if ev_ref.startswith("warnings:"):
                        for pair in ev_ref[len("warnings:"):].split(","):
                            reason = pair.split("=")[0]
                            if reason:
                                actual_covered.add(reason)
                for ev in f.k8s_events:
                    if ev.reason:
                        actual_covered.add(ev.reason)

            # (a) Every returned reason has count > threshold
            for reason, info in result:
                assert info["count"] > threshold, (
                    f"Returned reason {reason!r} has count={info['count']} "
                    f"which is not > threshold={threshold}"
                )

            # (b) No returned reason is covered by any finding
            for reason, _ in result:
                assert reason not in actual_covered, (
                    f"Returned reason {reason!r} is covered by a finding "
                    f"but should not appear in uncovered list"
                )

            # (c) Completeness: every warning reason NOT returned must be
            #     either <= threshold or covered
            for reason, count in reason_counts.items():
                if reason not in returned_reasons:
                    assert count <= threshold or reason in actual_covered, (
                        f"Reason {reason!r} (count={count}) is not returned, "
                        f"not covered, and count > threshold={threshold} — "
                        f"it should have been returned"
                    )

            # (d) Sorted by count descending
            counts = [info["count"] for _, info in result]
            assert counts == sorted(counts, reverse=True), (
                f"Result not sorted by count descending: {counts}"
            )

    @given(
        events=st.lists(
            _k8s_event(reason=_reason_pool, event_type=st.just("Warning")),
            min_size=1,
            max_size=30,
        ),
        threshold=st.integers(min_value=0, max_value=5),
    )
    @settings(max_examples=50)
    def test_empty_findings_returns_all_above_threshold(
        self, events: list[K8sEvent], threshold: int,
    ):
        """When findings list is empty, all reasons above threshold are returned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            watcher, store, run_id = _make_watcher(tmpdir)

            for ev in events:
                store.append_event(run_id, ev)

            result = watcher.get_uncovered_reasons([], threshold)
            returned_reasons = {r for r, _ in result}

            reason_counts = Counter(e.reason for e in events)
            expected = {r for r, c in reason_counts.items() if c > threshold}

            assert returned_reasons == expected, (
                f"With empty findings, expected reasons above threshold={threshold}: "
                f"{expected}, got: {returned_reasons}"
            )

    def test_no_events_returns_empty_list(self):
        """When no events exist, get_uncovered_reasons returns empty list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            watcher, _, _ = _make_watcher(tmpdir)
            result = watcher.get_uncovered_reasons([], threshold=0)
            assert result == []


# --- Unit tests for edge cases and enum ---


class TestUnitEdgeCases:
    """Unit tests for edge cases and enum values.

    Complements the property tests with deterministic checks for specific
    edge cases and the EVENT_ANALYSIS enum member.

    _Requirements: 1.4, 2.1_
    """

    def test_alert_type_event_analysis_exists(self):
        """AlertType.EVENT_ANALYSIS exists with value 'event_analysis'.

        **Validates: Requirements 2.1**
        """
        from k8s_scale_test.models import AlertType

        assert hasattr(AlertType, "EVENT_ANALYSIS")
        assert AlertType.EVENT_ANALYSIS.value == "event_analysis"

    def test_warning_summary_excludes_normal_events(self):
        """get_warning_summary() excludes Normal events from the result.

        Write a mix of Warning and Normal events with the same reason.
        Only Warning events should be counted.

        **Validates: Requirements 1.1**
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            watcher, store, run_id = _make_watcher(tmpdir)

            ts = datetime(2024, 6, 1, tzinfo=timezone.utc)

            # 3 Warning events for "Failed"
            for i in range(3):
                store.append_event(run_id, K8sEvent(
                    timestamp=ts,
                    namespace="default",
                    involved_object_kind="Pod",
                    involved_object_name=f"pod-{i}",
                    reason="Failed",
                    message="container start failed",
                    event_type="Warning",
                    scaling_step=0,
                    count=1,
                ))

            # 5 Normal events for "Failed" — should be excluded
            for i in range(5):
                store.append_event(run_id, K8sEvent(
                    timestamp=ts,
                    namespace="default",
                    involved_object_kind="Pod",
                    involved_object_name=f"pod-normal-{i}",
                    reason="Failed",
                    message="normal event",
                    event_type="Normal",
                    scaling_step=0,
                    count=1,
                ))

            # 2 Normal events for "Pulled" — Normal-only reason, should not appear
            for i in range(2):
                store.append_event(run_id, K8sEvent(
                    timestamp=ts,
                    namespace="default",
                    involved_object_kind="Pod",
                    involved_object_name=f"pod-pulled-{i}",
                    reason="Pulled",
                    message="pulled image",
                    event_type="Normal",
                    scaling_step=0,
                    count=1,
                ))

            summary = watcher.get_warning_summary()

            # Only "Failed" should appear, with count=3 (Warning only)
            assert "Failed" in summary
            assert summary["Failed"]["count"] == 3
            # Normal-only reason should not appear
            assert "Pulled" not in summary

    def test_uncovered_reasons_defaults_to_threshold_100(self):
        """get_uncovered_reasons() defaults to threshold=100.

        Generate 101 events for reason A (should be returned) and 99 events
        for reason B (should NOT be returned) when calling without explicit
        threshold.

        **Validates: Requirements 1.3**
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            watcher, store, run_id = _make_watcher(tmpdir)

            ts = datetime(2024, 6, 1, tzinfo=timezone.utc)

            # 101 Warning events for "HighVolume" — exceeds default threshold of 100
            for i in range(101):
                store.append_event(run_id, K8sEvent(
                    timestamp=ts,
                    namespace="default",
                    involved_object_kind="Pod",
                    involved_object_name=f"hv-pod-{i}",
                    reason="HighVolume",
                    message="high volume event",
                    event_type="Warning",
                    scaling_step=0,
                    count=1,
                ))

            # 99 Warning events for "LowVolume" — below default threshold of 100
            for i in range(99):
                store.append_event(run_id, K8sEvent(
                    timestamp=ts,
                    namespace="default",
                    involved_object_kind="Pod",
                    involved_object_name=f"lv-pod-{i}",
                    reason="LowVolume",
                    message="low volume event",
                    event_type="Warning",
                    scaling_step=0,
                    count=1,
                ))

            # Call WITHOUT explicit threshold — should default to 100
            result = watcher.get_uncovered_reasons([])
            returned_reasons = {r for r, _ in result}

            assert "HighVolume" in returned_reasons, (
                "HighVolume (101 events) should exceed default threshold of 100"
            )
            assert "LowVolume" not in returned_reasons, (
                "LowVolume (99 events) should not exceed default threshold of 100"
            )

    def test_uncovered_reasons_empty_when_all_covered(self):
        """get_uncovered_reasons() returns empty when all reasons are covered.

        **Validates: Requirements 1.2**
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            watcher, store, run_id = _make_watcher(tmpdir)

            ts = datetime(2024, 6, 1, tzinfo=timezone.utc)

            # Write 10 Warning events for "BackOff"
            for i in range(10):
                store.append_event(run_id, K8sEvent(
                    timestamp=ts,
                    namespace="default",
                    involved_object_kind="Pod",
                    involved_object_name=f"bo-pod-{i}",
                    reason="BackOff",
                    message="back-off restarting",
                    event_type="Warning",
                    scaling_step=0,
                    count=1,
                ))

            # Create a finding that covers "BackOff" via evidence_references
            finding = Finding(
                finding_id="f-1",
                timestamp=ts,
                severity=Severity.WARNING,
                symptom="pods restarting",
                affected_resources=[],
                k8s_events=[],
                node_metrics=[],
                node_diagnostics=[],
                evidence_references=["warnings:BackOff=10"],
            )

            # threshold=5 so BackOff (count=10) would qualify, but it's covered
            result = watcher.get_uncovered_reasons([finding], threshold=5)
            assert result == [], (
                "All reasons are covered by findings, result should be empty"
            )


# --- Controller event analysis integration tests ---


class TestControllerEventAnalysis:
    """Integration tests for the post-test event analysis block in Phase 7c.

    Verifies:
    - Cap at 3 investigations (Req 3.3)
    - Error handling continues on individual failure (Req 3.8)
    - EVENT_ANALYSIS log message appears in anomaly detector (Req 2.2)

    Uses source inspection for controller structural checks (matching the
    pattern in test_controller_tracing.py and test_controller_observability_prompt.py)
    and direct testing for the anomaly detector log message.

    _Requirements: 3.3, 3.8, 2.2_
    """

    def test_cap_at_3_investigations(self):
        """Controller caps event analysis at 3 investigations even when more uncovered reasons exist.

        The controller slices uncovered[:3] so only the first 3 reasons are
        investigated regardless of how many get_uncovered_reasons returns.

        **Validates: Requirements 3.3**
        """
        import inspect
        import k8s_scale_test.controller as ctrl_mod

        source = inspect.getsource(ctrl_mod.ScaleTestController.run)

        # The controller must slice uncovered to at most 3
        assert "uncovered[:3]" in source, (
            "Controller must cap event analysis at 3 investigations via uncovered[:3]"
        )

        # Verify it's used in a for loop (not just referenced)
        lines = source.split("\n")
        loop_lines = [l for l in lines if "uncovered[:3]" in l]
        assert len(loop_lines) >= 1
        assert "for " in loop_lines[0], (
            "uncovered[:3] should be used in a for loop"
        )

    def test_error_handling_continues_on_individual_failure(self):
        """Controller wraps each investigation in try/except so failures don't abort the loop.

        When handle_alert raises on one reason, the controller logs the error
        and continues to the next uncovered reason.

        **Validates: Requirements 3.8**
        """
        import inspect
        import k8s_scale_test.controller as ctrl_mod

        source = inspect.getsource(ctrl_mod.ScaleTestController.run)

        # Find the event analysis block — it should contain try/except inside the loop
        lines = source.split("\n")

        # Locate the for loop over uncovered[:3]
        loop_idx = None
        for i, line in enumerate(lines):
            if "uncovered[:3]" in line and "for " in line:
                loop_idx = i
                break
        assert loop_idx is not None, "Could not find 'for ... in uncovered[:3]' loop"

        # Extract the block after the loop (until the next dedented line at same or lower level)
        loop_indent = len(lines[loop_idx]) - len(lines[loop_idx].lstrip())
        block_lines = []
        for line in lines[loop_idx + 1:]:
            stripped = line.lstrip()
            if stripped == "":
                block_lines.append(line)
                continue
            current_indent = len(line) - len(stripped)
            if current_indent <= loop_indent:
                break
            block_lines.append(line)
        block = "\n".join(block_lines)

        # The block must contain try/except for error handling
        assert "try:" in block, (
            "Event analysis loop must contain try: for error handling"
        )
        assert "except Exception" in block, (
            "Event analysis loop must catch Exception to continue on failure"
        )

        # The except block should log the error
        assert "Event analysis failed for" in block, (
            "Error handler should log 'Event analysis failed for' message"
        )

    def test_event_analysis_log_message_in_anomaly_detector(self):
        """AnomalyDetector logs 'Post-test event analysis:' for EVENT_ANALYSIS alerts.

        **Validates: Requirements 2.2**
        """
        import inspect
        import k8s_scale_test.anomaly as anomaly_mod

        source = inspect.getsource(anomaly_mod.AnomalyDetector.handle_alert)

        # Verify the EVENT_ANALYSIS check exists
        assert "AlertType.EVENT_ANALYSIS" in source, (
            "handle_alert must check for AlertType.EVENT_ANALYSIS"
        )

        # Verify the distinct log message
        assert "Post-test event analysis:" in source, (
            "handle_alert must log 'Post-test event analysis:' for EVENT_ANALYSIS alerts"
        )

        # Verify the log is conditional on EVENT_ANALYSIS (not unconditional)
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if "AlertType.EVENT_ANALYSIS" in line:
                # The next non-empty line should contain the log message
                for j in range(i + 1, min(i + 3, len(lines))):
                    if "Post-test event analysis:" in lines[j]:
                        assert "log.info" in lines[j], (
                            "EVENT_ANALYSIS log should use log.info"
                        )
                        break
                break
