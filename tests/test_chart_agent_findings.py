"""Property-based test for chart agent finding markers (Task 11.2).

Feature: aws-observability-integration
Property 8: Chart renders agent finding markers when findings exist

**Validates: Requirements 9.2, 9.3**
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from k8s_scale_test.chart import generate_chart


# --- Strategies ---

_severities = st.sampled_from(["info", "warning", "critical"])
_sources = st.sampled_from(["proactive-scan", "reactive-investigation", "skeptical-review"])
_confidences = st.sampled_from(["high", "medium", "low"])


def _valid_agent_finding(base_time: datetime, offset_seconds: int) -> dict:
    """Build a valid agent finding dict at a specific time offset."""
    ts = base_time + timedelta(seconds=offset_seconds)
    return {
        "finding_id": f"agent-test-{offset_seconds}",
        "timestamp": ts.isoformat(),
        "source": "proactive-scan",
        "severity": "info",
        "title": "Test finding",
        "description": "Test description",
        "affected_resources": [],
        "evidence": [],
        "recommended_actions": [],
    }


def _agent_finding_strategy() -> st.SearchStrategy[dict]:
    """Generate a valid agent finding dict with random fields."""
    return st.fixed_dictionaries({
        "finding_id": st.text(
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
            min_size=1,
            max_size=40,
        ),
        "timestamp_offset": st.integers(min_value=5, max_value=300),
        "source": _sources,
        "severity": _severities,
        "title": st.text(min_size=1, max_size=80),
        "description": st.text(min_size=1, max_size=200),
        "affected_resources": st.lists(st.text(min_size=1, max_size=30), max_size=3),
        "evidence": st.just([]),
        "recommended_actions": st.lists(st.text(min_size=1, max_size=50), max_size=3),
        "has_review": st.booleans(),
        "review_confidence": _confidences,
    })


def _build_finding(raw: dict, base_time: datetime) -> dict:
    """Convert a strategy-generated dict into a proper agent finding."""
    ts = base_time + timedelta(seconds=raw["timestamp_offset"])
    finding = {
        "finding_id": raw["finding_id"],
        "timestamp": ts.isoformat(),
        "source": raw["source"],
        "severity": raw["severity"],
        "title": raw["title"],
        "description": raw["description"],
        "affected_resources": raw["affected_resources"],
        "evidence": raw["evidence"],
        "recommended_actions": raw["recommended_actions"],
    }
    if raw["has_review"]:
        finding["review"] = {
            "confidence": raw["review_confidence"],
            "reasoning": "Test reasoning",
            "alternative_explanations": [],
            "checkpoint_questions": [],
            "verification_results": [],
        }
    return finding


def _create_rate_data(run_dir: Path, num_points: int = 10) -> datetime:
    """Create minimal rate_data.jsonl for chart generation. Returns base time."""
    base_time = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    lines = []
    for i in range(num_points):
        ts = base_time + timedelta(seconds=i * 5)
        lines.append(json.dumps({
            "timestamp": ts.isoformat(),
            "ready_rate": 10.0 + i,
            "rolling_avg_rate": 10.0,
            "ready_count": i * 50,
            "pending_count": max(0, 100 - i * 10),
            "total_pods": i * 50 + max(0, 100 - i * 10),
            "interval_seconds": 5.0,
            "is_gap": False,
        }))
    (run_dir / "rate_data.jsonl").write_text("\n".join(lines))
    return base_time


class TestProperty8ChartAgentFindingMarkers:
    """Property 8: Chart renders agent finding markers when findings exist.

    **Validates: Requirements 9.2, 9.3**
    """

    @given(
        finding_raws=st.lists(_agent_finding_strategy(), min_size=0, max_size=10),
    )
    @settings(max_examples=100)
    def test_chart_annotation_markers_match_findings_count(
        self, finding_raws: list[dict]
    ):
        """Chart HTML contains correct number of annotation markers for findings.

        For non-empty findings: HTML contains annotation lines for each finding.
        For empty findings: HTML is generated successfully without annotations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            base_time = _create_rate_data(run_dir)

            # Write agent findings to findings/ directory
            findings_dir = run_dir / "findings"
            findings_dir.mkdir(exist_ok=True)

            findings = []
            for i, raw in enumerate(finding_raws):
                finding = _build_finding(raw, base_time)
                findings.append(finding)
                (findings_dir / f"agent-{i:04d}.json").write_text(
                    json.dumps(finding)
                )

            # Generate chart
            result = generate_chart(str(run_dir))
            assert result != "", "Chart generation should succeed"

            html = (run_dir / "chart.html").read_text()

            if not findings:
                # No findings -> no annotation config, empty annotationConfig
                assert "annotationConfig = {};" in html
                # No agent findings table
                assert "Agent Findings" not in html
            else:
                # Findings exist -> annotation lines present
                assert "annotationLines" in html
                assert "findingAnnotations.forEach" in html

                # The findingAnnotations JS array should contain data for each finding
                # Verify the JSON-serialized annotations are in the HTML
                assert "findingAnnotations" in html

                # Agent findings HTML table present
                assert "Agent Findings" in html

                # Severity colors are correct for each finding
                severity_colors = {
                    "info": "#3498db",
                    "warning": "#f39c12",
                    "critical": "#e74c3c",
                }
                for finding in findings:
                    sev = finding["severity"]
                    color = severity_colors[sev]
                    # Color appears in the findingAnnotations JS data
                    assert color in html

                # Verify the correct number of annotation entries by counting
                # occurrences in the serialized findingAnnotations array
                # Each finding produces one object with an "x" key in the array
                import re
                annotation_data_match = re.search(
                    r'const findingAnnotations = (\[.*?\]);',
                    html,
                    re.DOTALL,
                )
                assert annotation_data_match is not None
                annotation_data = json.loads(annotation_data_match.group(1))
                assert len(annotation_data) == len(findings)

    @given(
        finding_raws=st.lists(_agent_finding_strategy(), min_size=1, max_size=8),
    )
    @settings(max_examples=100)
    def test_chart_finding_titles_in_html(self, finding_raws: list[dict]):
        """Agent findings table rows match the number of findings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            base_time = _create_rate_data(run_dir)

            findings_dir = run_dir / "findings"
            findings_dir.mkdir(exist_ok=True)

            findings = []
            for i, raw in enumerate(finding_raws):
                finding = _build_finding(raw, base_time)
                findings.append(finding)
                (findings_dir / f"agent-{i:04d}.json").write_text(
                    json.dumps(finding)
                )

            result = generate_chart(str(run_dir))
            assert result != ""

            html = (run_dir / "chart.html").read_text()

            # Agent findings table should have one <tr> per finding (plus header row)
            import re
            table_match = re.search(
                r'Agent Findings</h2>\s*<table>(.*?)</table>',
                html,
                re.DOTALL,
            )
            assert table_match is not None
            table_html = table_match.group(1)
            # Count data rows (exclude the header <tr>)
            data_rows = re.findall(r'<tr><td', table_html)
            assert len(data_rows) == len(findings)

            # Each severity label appears in uppercase in the table
            for finding in findings:
                sev_upper = finding["severity"].upper()
                assert sev_upper in html

            # Review confidence shown when present
            for finding in findings:
                review = finding.get("review")
                if review:
                    confidence = review["confidence"]
                    assert confidence in html
