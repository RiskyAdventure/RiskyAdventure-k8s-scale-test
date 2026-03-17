"""Agent finding schema validation.

Validates that agent finding dicts conform to the expected schema
defined in the design document (Requirements 6.1, 6.2).
"""

REQUIRED_FINDING_FIELDS: set[str] = {
    "finding_id",
    "timestamp",
    "source",
    "severity",
    "title",
    "description",
    "affected_resources",
    "evidence",
    "recommended_actions",
}

REQUIRED_REVIEW_FIELDS: set[str] = {
    "confidence",
    "reasoning",
    "alternative_explanations",
    "checkpoint_questions",
    "verification_results",
}


def validate_agent_finding(data: dict) -> tuple[bool, list[str]]:
    """Validate an agent finding dict against the schema.

    Returns a tuple of (is_valid, errors) where errors is a list of
    human-readable error strings. An empty errors list means the finding
    is valid.
    """
    errors: list[str] = []

    # Check required top-level fields
    missing = REQUIRED_FINDING_FIELDS - set(data.keys())
    for field in sorted(missing):
        errors.append(f"Missing required field: {field}")

    # Validate the optional review field structure when present
    if "review" in data:
        review = data["review"]
        if not isinstance(review, dict):
            errors.append("Field 'review' must be a dict")
        else:
            missing_review = REQUIRED_REVIEW_FIELDS - set(review.keys())
            for field in sorted(missing_review):
                errors.append(f"Missing required review field: {field}")

    return (len(errors) == 0, errors)
