"""Property-based tests for agent finding schema validation (Task 2.2).

Feature: aws-observability-integration, Property 6: Agent finding schema validation
Validates: Requirements 6.1, 6.2
"""

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from k8s_scale_test.agent_schema import (
    REQUIRED_FINDING_FIELDS,
    REQUIRED_REVIEW_FIELDS,
    validate_agent_finding,
)

# --- Strategies ---

_sources = st.sampled_from(["proactive-scan", "reactive-investigation", "skeptical-review"])
_severities = st.sampled_from(["info", "warning", "critical"])
_confidences = st.sampled_from(["high", "medium", "low"])


def _complete_finding() -> st.SearchStrategy[dict]:
    """Generate a fully valid agent finding dict."""
    return st.fixed_dictionaries({
        "finding_id": st.text(min_size=1, max_size=50),
        "timestamp": st.text(min_size=1, max_size=30),
        "source": _sources,
        "severity": _severities,
        "title": st.text(min_size=1, max_size=100),
        "description": st.text(min_size=1, max_size=200),
        "affected_resources": st.lists(st.text(min_size=1, max_size=30), max_size=5),
        "evidence": st.lists(st.fixed_dictionaries({"source": st.text(min_size=1)}), max_size=3),
        "recommended_actions": st.lists(st.text(min_size=1, max_size=50), max_size=5),
    })


def _complete_review() -> st.SearchStrategy[dict]:
    """Generate a fully valid review field dict."""
    return st.fixed_dictionaries({
        "confidence": _confidences,
        "reasoning": st.text(min_size=1, max_size=100),
        "alternative_explanations": st.lists(st.text(min_size=1, max_size=50), max_size=3),
        "checkpoint_questions": st.lists(st.text(min_size=1, max_size=50), max_size=3),
        "verification_results": st.lists(
            st.fixed_dictionaries({"claim": st.text(min_size=1)}), max_size=3
        ),
    })


# --- Property Tests ---


class TestProperty6AgentFindingSchemaValidation:
    """Property 6: Agent finding schema validation.

    **Validates: Requirements 6.1, 6.2**
    """

    @given(data=_complete_finding())
    @settings(max_examples=100)
    def test_complete_finding_is_valid(self, data: dict):
        """A finding with all required fields passes validation."""
        is_valid, errors = validate_agent_finding(data)
        assert is_valid is True
        assert errors == []

    @given(
        data=_complete_finding(),
        fields_to_remove=st.lists(
            st.sampled_from(sorted(REQUIRED_FINDING_FIELDS)),
            min_size=1,
            unique=True,
        ),
    )
    @settings(max_examples=100)
    def test_missing_required_fields_detected(self, data: dict, fields_to_remove: list[str]):
        """Removing any subset of required fields causes validation failure."""
        for field in fields_to_remove:
            data.pop(field, None)

        is_valid, errors = validate_agent_finding(data)
        assert is_valid is False
        assert len(errors) == len(fields_to_remove)
        for field in fields_to_remove:
            assert any(field in e for e in errors)

    @given(data=_complete_finding(), review=_complete_review())
    @settings(max_examples=100)
    def test_finding_with_valid_review_is_valid(self, data: dict, review: dict):
        """A finding with all required fields plus a valid review passes."""
        data["review"] = review
        is_valid, errors = validate_agent_finding(data)
        assert is_valid is True
        assert errors == []

    @given(
        data=_complete_finding(),
        review=_complete_review(),
        review_fields_to_remove=st.lists(
            st.sampled_from(sorted(REQUIRED_REVIEW_FIELDS)),
            min_size=1,
            unique=True,
        ),
    )
    @settings(max_examples=100)
    def test_missing_review_fields_detected(
        self, data: dict, review: dict, review_fields_to_remove: list[str]
    ):
        """Removing any subset of review fields causes validation failure."""
        for field in review_fields_to_remove:
            review.pop(field, None)
        data["review"] = review

        is_valid, errors = validate_agent_finding(data)
        assert is_valid is False
        assert len(errors) == len(review_fields_to_remove)
        for field in review_fields_to_remove:
            assert any(field in e for e in errors)

    @given(data=_complete_finding())
    @settings(max_examples=100)
    def test_finding_without_review_is_valid(self, data: dict):
        """A finding without a review field is valid (review is optional)."""
        data.pop("review", None)
        is_valid, errors = validate_agent_finding(data)
        assert is_valid is True
        assert errors == []

    @given(data=_complete_finding(), bad_review=st.text(min_size=1))
    @settings(max_examples=100)
    def test_non_dict_review_is_invalid(self, data: dict, bad_review: str):
        """A review field that is not a dict causes validation failure."""
        data["review"] = bad_review
        is_valid, errors = validate_agent_finding(data)
        assert is_valid is False
        assert any("review" in e and "dict" in e for e in errors)
