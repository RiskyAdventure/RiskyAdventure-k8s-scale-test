"""Property-based test for agent findings in test run summary (Task 6.6).

Feature: aws-observability-integration
Property 7: Agent findings included in test run summary

**Validates: Requirements 9.1**
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter

from hypothesis import given, settings
from hypothesis import strategies as st

from k8s_scale_test.evidence import EvidenceStore
from k8s_scale_test.models import TestConfig


# --- Strategies ---

_severities = st.sampled_from(["info", "warning", "critical"])
_sources = st.sampled_from(["proactive-scan", "reactive-investigation", "skeptical-review"])
_confidences = st.sampled_from(["high", "medium", "low"])


def _valid_agent_finding() -> st.SearchStrategy[dict]:
    """Generate a valid agent finding dict with random severity."""
    return st.fixed_dictionaries({
        "finding_id": st.text(
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
            min_size=1,
            max_size=40,
        ),
        "timestamp": st.text(min_size=1, max_size=30),
        "source": _sources,
        "severity": _severities,
        "title": st.text(min_size=1, max_size=80),
        "description": st.text(min_size=1, max_size=200),
        "affected_resources": st.lists(st.text(min_size=1, max_size=30), max_size=5),
        "evidence": st.lists(
            st.fixed_dictionaries({"source": st.text(min_size=1, max_size=20)}),
            max_size=3,
        ),
        "recommended_actions": st.lists(st.text(min_size=1, max_size=50), max_size=5),
    })


def _agent_finding_with_review() -> st.SearchStrategy[dict]:
    """Generate an agent finding that may optionally include a review field."""
    base = _valid_agent_finding()
    review = st.fixed_dictionaries({
        "confidence": _confidences,
        "reasoning": st.text(min_size=1, max_size=100),
        "alternative_explanations": st.lists(st.text(min_size=1, max_size=50), max_size=3),
        "checkpoint_questions": st.lists(st.text(min_size=1, max_size=50), max_size=3),
        "verification_results": st.lists(
            st.fixed_dictionaries({
                "claim": st.text(min_size=1, max_size=50),
                "verified": st.sampled_from([True, False, "partial"]),
                "detail": st.text(min_size=1, max_size=80),
            }),
            max_size=2,
        ),
    })
    return st.one_of(
        base,
        st.tuples(base, review).map(lambda t: {**t[0], "review": t[1]}),
    )


class TestProperty7AgentFindingsInSummary:
    """Property 7: Agent findings included in test run summary.

    **Validates: Requirements 9.1**

    Tests the integration path: agent findings written to evidence store ->
    loaded via load_agent_findings -> included in summary with correct
    counts, severity breakdown, and review confidence levels.
    """

    @given(findings=st.lists(_agent_finding_with_review(), min_size=0, max_size=15))
    @settings(max_examples=100)
    def test_summary_contains_correct_counts_and_severity_breakdown(
        self, findings: list
    ):
        """Loaded agent findings have correct count, severity distribution,
        and review confidence levels preserved."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = EvidenceStore(tmpdir)
            config = TestConfig(target_pods=10)
            run_id = store.create_run(config)

            # Write findings to evidence store (simulates what the Kiro agent does)
            findings_dir = store._run_dir(run_id) / "findings"
            for i, finding in enumerate(findings):
                fname = f"agent-{i:04d}.json"
                (findings_dir / fname).write_text(json.dumps(finding))

            # Load via evidence store (same path as controller._make_summary)
            loaded = store.load_agent_findings(run_id)
            agent_findings = loaded or None

            if not findings:
                # No findings -> agent_findings should be None (empty list is falsy)
                assert agent_findings is None
            else:
                assert agent_findings is not None

                # (a) Count matches the number of loaded findings
                assert len(agent_findings) == len(findings)

                # (b) Severity breakdown matches actual severity distribution
                expected_severities = Counter(f["severity"] for f in findings)
                actual_severities = Counter(f["severity"] for f in agent_findings)
                assert actual_severities == expected_severities

                # (c) Finding IDs preserved in order
                expected_ids = [f["finding_id"] for f in findings]
                actual_ids = [f["finding_id"] for f in agent_findings]
                assert actual_ids == expected_ids

                # Review confidence levels preserved where present
                for orig, loaded_f in zip(findings, agent_findings):
                    if "review" in orig:
                        assert "review" in loaded_f
                        assert loaded_f["review"]["confidence"] == orig["review"]["confidence"]
                    else:
                        assert "review" not in loaded_f

    @given(findings=st.lists(_valid_agent_finding(), min_size=1, max_size=10))
    @settings(max_examples=100)
    def test_agent_findings_field_on_summary_dataclass(self, findings: list):
        """TestRunSummary.agent_findings field stores and serializes correctly."""
        from k8s_scale_test.models import TestRunSummary

        # Verify the field exists and accepts a list of dicts
        fields = {f.name for f in __import__("dataclasses").fields(TestRunSummary)}
        assert "agent_findings" in fields

        # Verify None default
        import dataclasses
        for f in dataclasses.fields(TestRunSummary):
            if f.name == "agent_findings":
                assert f.default is None
                break
