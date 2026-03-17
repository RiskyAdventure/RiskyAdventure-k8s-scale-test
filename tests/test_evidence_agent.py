"""Property-based tests for Evidence Store agent extensions (Tasks 3.2, 3.3).

Feature: aws-observability-integration
Property 4: Agent findings round-trip through Evidence Store
Property 5: Malformed agent findings are skipped gracefully
"""

import json
import logging
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from k8s_scale_test.evidence import EvidenceStore

# --- Strategies ---

_sources = st.sampled_from(["proactive-scan", "reactive-investigation", "skeptical-review"])
_severities = st.sampled_from(["info", "warning", "critical"])


def _valid_finding() -> st.SearchStrategy[dict]:
    """Generate a valid agent finding dict."""
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


def _malformed_content() -> st.SearchStrategy[str]:
    """Generate content that is NOT valid JSON."""
    return st.one_of(
        st.just("{not json"),
        st.just(""),
        st.just("{{{{"),
        st.text(min_size=1, max_size=50).filter(lambda s: _is_not_valid_json(s)),
    )


def _is_not_valid_json(s: str) -> bool:
    try:
        json.loads(s)
        return False
    except (json.JSONDecodeError, ValueError):
        return True


# --- Property 4: Agent findings round-trip ---


class TestProperty4AgentFindingsRoundTrip:
    """Property 4: Agent findings round-trip through Evidence Store.

    **Validates: Requirements 6.3, 6.4**
    """

    @given(findings=st.lists(_valid_finding(), min_size=0, max_size=10))
    @settings(max_examples=100)
    def test_round_trip_preserves_all_findings(self, findings: list[dict]):
        """Writing N valid agent-*.json files and loading returns exactly N parsed dicts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = EvidenceStore(tmpdir)
            run_id = store.create_run(
                _dummy_config()
            )

            # Write each finding as agent-{finding_id}.json
            findings_dir = store._run_dir(run_id) / "findings"
            for i, finding in enumerate(findings):
                fname = f"agent-{i}.json"
                (findings_dir / fname).write_text(json.dumps(finding))

            loaded = store.load_agent_findings(run_id)

            assert len(loaded) == len(findings)
            # Verify each finding is present (order matches sorted filenames)
            for i, finding in enumerate(findings):
                assert loaded[i] == finding


# --- Property 5: Malformed agent findings are skipped ---


class TestProperty5MalformedAgentFindings:
    """Property 5: Malformed agent findings are skipped gracefully.

    **Validates: Requirements 6.6, 11.5**
    """

    @given(
        valid_findings=st.lists(_valid_finding(), min_size=0, max_size=5),
        malformed_count=st.integers(min_value=1, max_value=5),
        malformed_content=st.lists(
            _malformed_content(), min_size=1, max_size=5,
        ),
    )
    @settings(max_examples=100)
    def test_malformed_files_skipped_valid_returned(
        self,
        valid_findings: list[dict],
        malformed_count: int,
        malformed_content: list[str],
    ):
        """A mix of valid and malformed agent-*.json files returns only valid ones."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = EvidenceStore(tmpdir)
            run_id = store.create_run(_dummy_config())

            findings_dir = store._run_dir(run_id) / "findings"

            # Write valid findings with names that sort first (agent-v000.json, etc.)
            for i, finding in enumerate(valid_findings):
                fname = f"agent-v{i:03d}.json"
                (findings_dir / fname).write_text(json.dumps(finding))

            # Write malformed files with names that sort after (agent-z000.json, etc.)
            actual_malformed = malformed_content[:malformed_count]
            for i, content in enumerate(actual_malformed):
                fname = f"agent-z{i:03d}.json"
                (findings_dir / fname).write_text(content)

            with _capture_warnings() as warnings:
                loaded = store.load_agent_findings(run_id)

            # Only valid findings returned
            assert len(loaded) == len(valid_findings)
            for i, finding in enumerate(valid_findings):
                assert loaded[i] == finding

            # Malformed files produced warnings
            if actual_malformed:
                assert any("Skipping malformed agent finding" in w for w in warnings)

    @given(findings=st.lists(_valid_finding(), min_size=1, max_size=5))
    @settings(max_examples=100)
    def test_no_exception_on_mixed_content(self, findings: list[dict]):
        """load_agent_findings never raises, even with malformed files present."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = EvidenceStore(tmpdir)
            run_id = store.create_run(_dummy_config())

            findings_dir = store._run_dir(run_id) / "findings"

            # Write one valid, one malformed
            (findings_dir / "agent-good.json").write_text(json.dumps(findings[0]))
            (findings_dir / "agent-bad.json").write_text("{broken json")

            # Should not raise
            loaded = store.load_agent_findings(run_id)
            assert len(loaded) == 1
            assert loaded[0] == findings[0]


# --- Helper ---

def _dummy_config():
    """Create a minimal TestConfig for creating a run directory."""
    from k8s_scale_test.models import TestConfig
    return TestConfig(target_pods=10)


from contextlib import contextmanager


@contextmanager
def _capture_warnings():
    """Capture log.warning messages from the evidence module."""
    captured = []
    handler = logging.Handler()
    handler.emit = lambda record: captured.append(record.getMessage())
    handler.setLevel(logging.WARNING)
    logger = logging.getLogger("k8s_scale_test.evidence")
    logger.addHandler(handler)
    try:
        yield captured
    finally:
        logger.removeHandler(handler)
