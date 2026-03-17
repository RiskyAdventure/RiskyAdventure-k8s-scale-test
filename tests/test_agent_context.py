"""Property-based tests for ContextFileWriter (Tasks 4.2, 4.3, 4.4).

Feature: aws-observability-integration
Property 1: Context file correctly maps TestConfig fields
Property 2: Context file phase updates are consistent
Property 3: Context file appends accumulate correctly
"""

import tempfile
from datetime import datetime, timezone

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from k8s_scale_test.agent_context import ContextFileWriter
from k8s_scale_test.evidence import EvidenceStore
from k8s_scale_test.models import Alert, AlertType, Finding, Severity, TestConfig


# --- Strategies ---

_optional_str = st.one_of(st.none(), st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_./"),
    min_size=1,
    max_size=60,
))

_namespace = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-"),
    min_size=1,
    max_size=30,
)

_node_name = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_."),
    min_size=1,
    max_size=40,
)

_phases = st.sampled_from(["initializing", "scaling", "hold-at-peak", "cleanup", "complete"])

_timestamps = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 12, 31),
    timezones=st.just(timezone.utc),
)

_alert_types = st.sampled_from(list(AlertType))
_severities = st.sampled_from(list(Severity))


def _test_config(
    amp=_optional_str,
    cw=_optional_str,
    eks=_optional_str,
    target_pods=st.integers(min_value=1, max_value=100000),
) -> st.SearchStrategy[TestConfig]:
    """Generate a TestConfig with random observability fields."""
    return st.builds(
        TestConfig,
        target_pods=target_pods,
        amp_workspace_id=amp,
        cloudwatch_log_group=cw,
        eks_cluster_name=eks,
    )


def _alert() -> st.SearchStrategy[Alert]:
    """Generate a random Alert."""
    return st.builds(
        Alert,
        alert_type=_alert_types,
        timestamp=_timestamps,
        message=st.text(min_size=1, max_size=80),
        context=st.fixed_dictionaries({
            "current_rate": st.floats(min_value=0, max_value=1000, allow_nan=False),
            "rolling_avg": st.floats(min_value=0, max_value=1000, allow_nan=False),
            "ready_count": st.integers(min_value=0, max_value=100000),
            "pending_count": st.integers(min_value=0, max_value=100000),
        }),
    )


def _finding() -> st.SearchStrategy[Finding]:
    """Generate a random Finding."""
    return st.builds(
        Finding,
        finding_id=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
            min_size=1,
            max_size=40,
        ),
        timestamp=_timestamps,
        severity=_severities,
        symptom=st.text(min_size=1, max_size=80),
        affected_resources=st.lists(_node_name, min_size=0, max_size=20),
        k8s_events=st.just([]),
        node_metrics=st.just([]),
        node_diagnostics=st.just([]),
        evidence_references=st.just([]),
    )


# --- Property 1: Context file correctly maps TestConfig fields ---


class TestProperty1ContextFileConfigMapping:
    """Property 1: Context file correctly maps TestConfig fields.

    **Validates: Requirements 2.1, 2.6, 8.2, 8.3, 8.4, 8.5**
    """

    @given(
        config=_test_config(),
        namespaces=st.lists(_namespace, min_size=1, max_size=10),
        node_list=st.lists(_node_name, min_size=0, max_size=10),
    )
    @settings(max_examples=100)
    def test_config_fields_mapped_correctly(
        self, config: TestConfig, namespaces: list[str], node_list: list[str]
    ):
        """Non-None config fields appear with exact values; None fields are absent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = EvidenceStore(tmpdir)
            run_id = store.create_run(config)

            writer = ContextFileWriter(store, run_id, config)
            writer.write_initial_context(namespaces, node_list)

            ctx = store.load_agent_context(run_id)
            assert ctx is not None

            # Always-present fields
            assert ctx["run_id"] == run_id
            assert ctx["target_pods"] == config.target_pods
            assert ctx["namespaces"] == namespaces
            assert ctx["current_phase"] == "initializing"
            assert ctx["alerts"] == []
            assert ctx["finding_summaries"] == []
            assert "evidence_dir" in ctx
            assert "test_start" in ctx
            assert "phase_start" in ctx

            # Observability fields: present iff not None, with exact value
            _check_optional_field(ctx, "amp_workspace_id", config.amp_workspace_id)
            _check_optional_field(ctx, "cloudwatch_log_group", config.cloudwatch_log_group)
            _check_optional_field(ctx, "eks_cluster_name", config.eks_cluster_name)


# --- Property 2: Context file phase updates are consistent ---


class TestProperty2ContextFilePhaseUpdates:
    """Property 2: Context file phase updates are consistent.

    **Validates: Requirements 2.2**
    """

    @given(
        config=_test_config(),
        namespaces=st.lists(_namespace, min_size=1, max_size=5),
        phase_sequence=st.lists(
            st.tuples(_phases, _timestamps),
            min_size=1,
            max_size=10,
        ),
    )
    @settings(max_examples=100)
    def test_phase_updates_preserve_other_fields(
        self,
        config: TestConfig,
        namespaces: list[str],
        phase_sequence: list[tuple[str, datetime]],
    ):
        """Each update_phase sets phase/timestamp correctly and preserves other fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = EvidenceStore(tmpdir)
            run_id = store.create_run(config)

            writer = ContextFileWriter(store, run_id, config)
            writer.write_initial_context(namespaces, [])

            # Capture initial context for comparison of stable fields
            initial_ctx = store.load_agent_context(run_id)
            assert initial_ctx is not None

            for phase, ts in phase_sequence:
                writer.update_phase(phase, ts)

                ctx = store.load_agent_context(run_id)
                assert ctx is not None

                # Phase fields updated correctly
                assert ctx["current_phase"] == phase
                assert ctx["phase_start"] == ts.isoformat()

                # Other fields preserved
                assert ctx["run_id"] == initial_ctx["run_id"]
                assert ctx["target_pods"] == initial_ctx["target_pods"]
                assert ctx["namespaces"] == initial_ctx["namespaces"]
                assert ctx["alerts"] == initial_ctx["alerts"]
                assert ctx["finding_summaries"] == initial_ctx["finding_summaries"]
                assert ctx["evidence_dir"] == initial_ctx["evidence_dir"]
                assert ctx["test_start"] == initial_ctx["test_start"]


# --- Property 3: Context file appends accumulate correctly ---


class TestProperty3ContextFileAppends:
    """Property 3: Context file appends accumulate correctly.

    **Validates: Requirements 2.3, 2.4**
    """

    @given(
        config=_test_config(),
        namespaces=st.lists(_namespace, min_size=1, max_size=3),
        operations=st.lists(
            st.one_of(
                st.tuples(st.just("alert"), _alert()),
                st.tuples(st.just("finding"), _finding()),
            ),
            min_size=0,
            max_size=15,
        ),
    )
    @settings(max_examples=100)
    def test_appends_accumulate_in_order(
        self,
        config: TestConfig,
        namespaces: list[str],
        operations: list,
    ):
        """Interleaved alerts and findings accumulate with correct counts and order."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = EvidenceStore(tmpdir)
            run_id = store.create_run(config)

            writer = ContextFileWriter(store, run_id, config)
            writer.write_initial_context(namespaces, [])

            expected_alert_count = 0
            expected_finding_count = 0

            for op_type, item in operations:
                if op_type == "alert":
                    writer.append_alert(item)
                    expected_alert_count += 1
                else:
                    writer.append_finding_summary(item)
                    expected_finding_count += 1

            ctx = store.load_agent_context(run_id)
            assert ctx is not None

            assert len(ctx["alerts"]) == expected_alert_count
            assert len(ctx["finding_summaries"]) == expected_finding_count

            # Verify ordering matches append order
            alert_idx = 0
            finding_idx = 0
            for op_type, item in operations:
                if op_type == "alert":
                    stored = ctx["alerts"][alert_idx]
                    assert stored["timestamp"] == item.timestamp.isoformat()
                    assert stored["message"] == item.message
                    assert stored["current_rate"] == item.context["current_rate"]
                    assert stored["rolling_avg"] == item.context["rolling_avg"]
                    assert stored["ready_count"] == item.context["ready_count"]
                    assert stored["pending_count"] == item.context["pending_count"]
                    alert_idx += 1
                else:
                    stored = ctx["finding_summaries"][finding_idx]
                    assert stored["finding_id"] == item.finding_id
                    assert stored["severity"] == item.severity.value
                    assert stored["symptom"] == item.symptom
                    assert stored["affected_resources"] == item.affected_resources[:10]
                    finding_idx += 1


# --- Helpers ---

def _check_optional_field(ctx: dict, field_name: str, config_value) -> None:
    """Assert optional field is present with value if not None, absent if None."""
    if config_value is not None:
        assert field_name in ctx, f"{field_name} should be present when config value is set"
        assert ctx[field_name] == config_value
    else:
        assert field_name not in ctx, f"{field_name} should be absent when config value is None"
