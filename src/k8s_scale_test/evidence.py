"""Evidence store for persisting test run artifacts."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from k8s_scale_test.models import (
    Finding,
    K8sEvent,
    NodeDiagnostic,
    PreflightReport,
    RateDataPoint,
    TestConfig,
    TestRunSummary,
)

log = logging.getLogger(__name__)


class EvidenceStore:
    """Persists all test artifacts to disk in JSON/JSONL format.

    Every test run gets its own directory under ``output_dir``. The
    directory layout is designed so each artifact type is easy to find
    and can be loaded independently:

    Directory layout per run::

        {output_dir}/
        └── {run_id}/                      # e.g. "2024-03-15_14-30-00"
            ├── config.json                # TestConfig snapshot at run start
            ├── preflight.json             # Capacity validation results
            ├── rate_data.jsonl            # One JSON line per 5s tick (pod ready rate)
            ├── events.jsonl               # K8s Warning events (append-only)
            ├── summary.json               # Final test run summary
            ├── cl2_summary.json           # ClusterLoader2 results (if CL2 was run)
            ├── agent_context.json         # AI sub-agent context file
            ├── observer.log               # Independent pod-count observer (CSV)
            ├── findings/                  # Anomaly investigation results
            │   ├── finding-{id}.json      # One file per investigation
            │   └── agent-{id}.json        # AI agent findings (if any)
            └── diagnostics/               # Node-level SSM command outputs
                ├── {node}_{timestamp}.json # Per-node diagnostic snapshots
                └── health_sweep.json      # Peak-load health sweep results

    File formats:
    - ``.json`` files: pretty-printed (indent=2) for human readability.
    - ``.jsonl`` files: one JSON object per line, append-only. Used for
      high-frequency data (rate ticks every 5s, events) where we can't
      afford to rewrite the whole file on each append.
    """

    def __init__(self, output_dir: str) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def create_run(self, config: TestConfig) -> str:
        """Create a new run directory and save the initial config snapshot.

        Returns a run ID in the format ``YYYY-MM-DD_HH-MM-SS`` (UTC).
        Creates the ``findings/`` and ``diagnostics/`` subdirectories
        so other methods can write to them immediately.
        """
        now = datetime.now(timezone.utc)
        run_id = now.strftime("%Y-%m-%d_%H-%M-%S")
        run_dir = self.output_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "findings").mkdir(exist_ok=True)
        (run_dir / "diagnostics").mkdir(exist_ok=True)
        self._write_json(run_dir / "config.json", config.to_dict())
        return run_id

    def _run_dir(self, run_id: str) -> Path:
        return self.output_dir / run_id

    # ------------------------------------------------------------------
    # Writers
    # ------------------------------------------------------------------

    def save_preflight_report(self, run_id: str, report: PreflightReport) -> None:
        self._write_json(self._run_dir(run_id) / "preflight.json", report.to_dict())

    def append_rate_datapoint(self, run_id: str, datapoint: RateDataPoint) -> None:
        self._append_jsonl(self._run_dir(run_id) / "rate_data.jsonl", datapoint.to_dict())

    def append_event(self, run_id: str, event: K8sEvent) -> None:
        self._append_jsonl(self._run_dir(run_id) / "events.jsonl", event.to_dict())

    def save_finding(self, run_id: str, finding: Finding) -> None:
        path = self._run_dir(run_id) / "findings" / f"{finding.finding_id}.json"
        self._write_json(path, finding.to_dict())

    def save_node_diagnostic(self, run_id: str, diagnostic: NodeDiagnostic) -> None:
        ts = diagnostic.collection_timestamp.strftime("%Y%m%d%H%M%S")
        fname = f"{diagnostic.node_name}_{ts}.json"
        path = self._run_dir(run_id) / "diagnostics" / fname
        self._write_json(path, diagnostic.to_dict())

    def save_summary(self, run_id: str, summary: TestRunSummary) -> None:
        self._write_json(self._run_dir(run_id) / "summary.json", summary.to_dict())

    def save_cl2_summary(self, run_id: str, summary) -> None:
        """Save CL2 results as cl2_summary.json in the run directory."""
        self._write_json(self._run_dir(run_id) / "cl2_summary.json", summary.to_dict())

    def save_scanner_finding(self, run_id: str, result) -> None:
        """Append a scanner finding to scanner_findings.jsonl."""
        entry = {
            "query_name": result.query_name,
            "severity": result.severity.value,
            "title": result.title,
            "detail": result.detail,
            "source": result.source.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._append_jsonl(self._run_dir(run_id) / "scanner_findings.jsonl", entry)

    def write_agent_context(self, run_id: str, context: dict) -> None:
        """Write agent_context.json to the run directory."""
        self._write_json(self._run_dir(run_id) / "agent_context.json", context)

    def load_agent_context(self, run_id: str) -> dict | None:
        """Load agent_context.json if it exists. Returns None if not present."""
        path = self._run_dir(run_id) / "agent_context.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception as exc:
            log.warning("Failed to load agent context from %s: %s", path, exc)
            return None

    def load_agent_findings(self, run_id: str) -> list[dict]:
        """Load all agent-*.json files from findings/ directory.

        Skips malformed files with a warning log. Returns list of parsed dicts.
        """
        findings_dir = self._run_dir(run_id) / "findings"
        if not findings_dir.exists():
            return []
        results: list[dict] = []
        for f in sorted(findings_dir.glob("agent-*.json")):
            try:
                results.append(json.loads(f.read_text()))
            except Exception as exc:
                log.warning("Skipping malformed agent finding %s: %s", f, exc)
        return results


    def load_cl2_summary(self, run_id: str):
        """Load CL2 summary if it exists. Returns None if not present."""
        import json as _json
        from k8s_scale_test.models import CL2Summary
        path = self._run_dir(run_id) / "cl2_summary.json"
        if not path.exists():
            return None
        data = _json.loads(path.read_text())
        return CL2Summary.from_dict(data)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query_events(
        self,
        run_id: str,
        namespace: str | None = None,
        reason: str | None = None,
        object_kind: str | None = None,
        object_name: str | None = None,
    ) -> list[K8sEvent]:
        """Filter stored events by optional criteria. All filters are AND-ed.

        Reads events.jsonl line by line (no full-file load) so this works
        even for runs with tens of thousands of events.

        Parameters
        ----------
        run_id : str
            Test run identifier.
        namespace, reason, object_kind, object_name : str, optional
            If provided, only events matching ALL specified filters are returned.

        Returns
        -------
        list[K8sEvent]
            Matching events, or empty list if events.jsonl doesn't exist.
        """
        events_path = self._run_dir(run_id) / "events.jsonl"
        if not events_path.exists():
            return []

        results: list[K8sEvent] = []
        with open(events_path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if namespace is not None and data.get("namespace") != namespace:
                    continue
                if reason is not None and data.get("reason") != reason:
                    continue
                if object_kind is not None and data.get("involved_object_kind") != object_kind:
                    continue
                if object_name is not None and data.get("involved_object_name") != object_name:
                    continue
                results.append(K8sEvent.from_dict(data))
        return results

    # ------------------------------------------------------------------
    # Loader
    # ------------------------------------------------------------------

    def load_run(self, run_id: str) -> dict:
        """Load all artifacts for a run as a dict of raw JSON data.

        Returns a dict with keys like "config", "preflight", "summary",
        "rate_data" (list), "events" (list), "findings" (list),
        "diagnostics" (list). Missing files are simply omitted from the dict.
        """
        rd = self._run_dir(run_id)
        data: dict = {}

        for name in ("config.json", "preflight.json", "summary.json", "cl2_summary.json"):
            p = rd / name
            if p.exists():
                data[name.replace(".json", "")] = json.loads(p.read_text())

        # JSONL files
        for name in ("rate_data.jsonl", "events.jsonl"):
            p = rd / name
            key = name.replace(".jsonl", "")
            if p.exists():
                data[key] = [
                    json.loads(line)
                    for line in p.read_text().splitlines()
                    if line.strip()
                ]

        # Findings
        findings_dir = rd / "findings"
        if findings_dir.exists():
            data["findings"] = [
                json.loads(f.read_text())
                for f in sorted(findings_dir.glob("*.json"))
            ]

        # Diagnostics
        diag_dir = rd / "diagnostics"
        if diag_dir.exists():
            data["diagnostics"] = [
                json.loads(f.read_text())
                for f in sorted(diag_dir.glob("*.json"))
            ]

        return data

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        try:
            path.write_text(json.dumps(data, indent=2, default=str))
        except Exception as exc:
            log.error("Failed to write %s: %s", path, exc)

    @staticmethod
    def _append_jsonl(path: Path, data: dict) -> None:
        try:
            with open(path, "a") as fh:
                fh.write(json.dumps(data, default=str) + "\n")
        except Exception as exc:
            log.error("Failed to append to %s: %s", path, exc)
