"""Ingestion pipeline — converts resolved Findings into KB entries.

Extracts signatures from Finding K8s events and node diagnostics,
checks for duplicates via SignatureMatcher, and either updates an
existing entry or creates a new one.  Skeptical review confidence
gates entry creation (high → active, medium → pending, low → skip).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from k8s_scale_test.kb_matcher import SignatureMatcher
from k8s_scale_test.kb_store import KBStore
from k8s_scale_test.models import (
    AffectedVersions,
    Finding,
    KBEntry,
    NodeDiagnostic,
    Severity,
    Signature,
)

logger = logging.getLogger(__name__)

# Patterns that indicate error/warning lines worth capturing as log_patterns
_ERROR_LINE_RE = re.compile(
    r"(?i)(error|fail|fatal|panic|oom|kill|timeout|throttl|refused|denied|"
    r"evict|pressure|crash|backoff|unhealthy|unready|not\s+ready|"
    r"cannot|unable|exceeded|exhausted)",
)


class IngestionPipeline:
    """Converts resolved Findings into candidate KB entries."""

    def __init__(self, kb_store: KBStore, matcher: SignatureMatcher) -> None:
        self._kb_store = kb_store
        self._matcher = matcher

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest_finding(
        self,
        finding: Finding,
        review: Optional[Dict[str, Any]] = None,
    ) -> Optional[KBEntry]:
        """Process a single resolved Finding into a KB entry.

        Returns the created/updated KBEntry, or None if skipped.
        """
        # Skip unresolved findings
        if finding.root_cause is None:
            logger.debug("Skipping finding %s: no root_cause", finding.finding_id)
            return None

        # --- Determine status from review confidence ---
        status = "pending"  # default when no review
        review_notes: Optional[str] = None
        alternative_explanations: List[str] = []
        checkpoint_questions: List[str] = []

        if review is not None:
            confidence = review.get("confidence", "").lower()
            if confidence == "high":
                status = "active"
            elif confidence == "medium":
                status = "pending"
                review_notes = review.get("review_notes")
                alternative_explanations = review.get("alternative_explanations", [])
                checkpoint_questions = review.get("checkpoint_questions", [])
            elif confidence == "low":
                reason = review.get("review_notes", "low confidence review")
                logger.info(
                    "Skipping finding %s: low confidence — %s",
                    finding.finding_id,
                    reason,
                )
                return None
            # Any other confidence value: treat as no review → pending

        # --- Extract signature ---
        signature = self._extract_signature(finding)

        # --- Check for duplicate ---
        existing_entries = self._kb_store.load_all()
        matches = self._matcher.match(
            finding.k8s_events,
            finding.node_diagnostics,
            existing_entries,
        )

        if matches:
            best_entry, best_score = matches[0]
            logger.info(
                "Finding %s matches existing KB entry %s (score=%.2f), updating occurrence",
                finding.finding_id,
                best_entry.entry_id,
                best_score,
            )
            self._kb_store.update_occurrence(
                best_entry.entry_id, datetime.now(timezone.utc)
            )
            return best_entry

        # --- Create new entry ---
        entry_id = self._generate_entry_id(finding.root_cause)
        category = self._infer_category(finding)
        now = datetime.now(timezone.utc)

        entry = KBEntry(
            entry_id=entry_id,
            title=self._generate_title(finding),
            category=category,
            signature=signature,
            root_cause=finding.root_cause,
            recommended_actions=[],
            severity=Severity.WARNING,
            affected_versions=[],
            created_at=now,
            last_seen=now,
            occurrence_count=1,
            status=status,
            review_notes=review_notes,
            alternative_explanations=alternative_explanations,
            checkpoint_questions=checkpoint_questions,
        )

        self._kb_store.save(entry)
        logger.info("Created new KB entry %s from finding %s", entry_id, finding.finding_id)
        return entry

    def ingest_run(
        self,
        run_dir: str,
        evidence_store=None,
    ) -> List[KBEntry]:
        """Load all finding JSON files from a run directory and ingest them.

        Looks in ``{run_dir}/findings/`` for regular findings and
        ``{run_dir}/agent_findings/`` for agent-produced findings.
        """
        results: List[KBEntry] = []
        run_path = Path(run_dir)

        # --- Load regular findings ---
        findings_dir = run_path / "findings"
        if findings_dir.is_dir():
            for fpath in sorted(findings_dir.glob("*.json")):
                entry = self._load_and_ingest_finding(fpath)
                if entry is not None:
                    results.append(entry)

        # --- Load agent findings ---
        agent_dir = run_path / "agent_findings"
        if agent_dir.is_dir():
            for fpath in sorted(agent_dir.glob("*.json")):
                entry = self._load_and_ingest_finding(fpath)
                if entry is not None:
                    results.append(entry)

        logger.info(
            "Ingested %d entries from run directory %s", len(results), run_dir
        )
        return results

    # ------------------------------------------------------------------
    # Signature extraction
    # ------------------------------------------------------------------

    def _extract_signature(self, finding: Finding) -> Signature:
        """Build a Signature from a Finding's events and diagnostics."""
        event_reasons = self._extract_event_reasons(finding)
        log_patterns = self._extract_log_patterns(finding)
        resource_kinds = self._extract_resource_kinds(finding)

        return Signature(
            event_reasons=event_reasons,
            log_patterns=log_patterns,
            metric_conditions=[],
            resource_kinds=resource_kinds,
        )

    @staticmethod
    def _extract_event_reasons(finding: Finding) -> List[str]:
        """Unique event reasons from the Finding's K8s events."""
        seen: set[str] = set()
        reasons: List[str] = []
        for ev in finding.k8s_events:
            if ev.reason and ev.reason not in seen:
                seen.add(ev.reason)
                reasons.append(ev.reason)
        return reasons

    @staticmethod
    def _extract_log_patterns(finding: Finding) -> List[str]:
        """Extract distinctive error/warning substrings from SSM output."""
        patterns: List[str] = []
        seen: set[str] = set()

        for diag in finding.node_diagnostics:
            for ssm_result in (
                diag.kubelet_logs,
                diag.containerd_logs,
                diag.journal_kubelet,
                diag.journal_containerd,
                diag.resource_utilization,
            ):
                if ssm_result is None or not ssm_result.output:
                    continue
                for line in ssm_result.output.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if _ERROR_LINE_RE.search(line):
                        # Extract a distinctive substring — take the core
                        # message portion (skip timestamps/prefixes).
                        snippet = _extract_distinctive_snippet(line)
                        if snippet and snippet not in seen:
                            seen.add(snippet)
                            patterns.append(snippet)

        return patterns

    @staticmethod
    def _extract_resource_kinds(finding: Finding) -> List[str]:
        """Unique involved_object_kind values from K8s events."""
        seen: set[str] = set()
        kinds: List[str] = []
        for ev in finding.k8s_events:
            kind = ev.involved_object_kind
            if kind and kind not in seen:
                seen.add(kind)
                kinds.append(kind)
        return kinds

    # ------------------------------------------------------------------
    # Entry metadata helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_entry_id(root_cause: str) -> str:
        """Create a slug-style entry_id from the root cause text."""
        # Take first ~60 chars, slugify
        slug = re.sub(r"[^a-z0-9]+", "-", root_cause[:60].lower()).strip("-")
        if not slug:
            slug = "finding"
        # Append short UUID suffix to ensure uniqueness
        short_uuid = uuid.uuid4().hex[:8]
        return f"{slug}-{short_uuid}"

    @staticmethod
    def _generate_title(finding: Finding) -> str:
        """Create a human-readable title from the finding."""
        if finding.root_cause:
            # Use first sentence or first 80 chars of root cause
            first_sentence = finding.root_cause.split(".")[0]
            if len(first_sentence) > 80:
                return first_sentence[:77] + "..."
            return first_sentence
        return finding.symptom[:80] if finding.symptom else f"Finding {finding.finding_id}"

    @staticmethod
    def _infer_category(finding: Finding) -> str:
        """Infer a KB category from the finding's events and symptom."""
        text = " ".join(
            [finding.symptom or ""]
            + [finding.root_cause or ""]
            + [ev.reason for ev in finding.k8s_events]
            + [ev.message for ev in finding.k8s_events]
        ).lower()

        if any(kw in text for kw in ("network", "cni", "eni", "mac", "ip ", "dns", "coredns")):
            return "networking"
        if any(kw in text for kw in ("schedule", "scheduling", "pending", "unschedulable")):
            return "scheduling"
        if any(kw in text for kw in ("capacity", "insufficient", "karpenter", "nodeclaim")):
            return "capacity"
        if any(kw in text for kw in ("disk", "volume", "storage", "nvme", "pvc")):
            return "storage"
        if any(kw in text for kw in ("control-plane", "apiserver", "etcd", "webhook")):
            return "control-plane"
        # Default to runtime
        return "runtime"

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------

    def _load_and_ingest_finding(self, fpath: Path) -> Optional[KBEntry]:
        """Load a single finding JSON file and ingest it."""
        try:
            data = json.loads(fpath.read_text())
            finding = Finding.from_dict(data)
            return self.ingest_finding(finding)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Skipping malformed finding file %s: %s", fpath, exc)
            return None
        except Exception as exc:
            logger.warning("Error ingesting finding from %s: %s", fpath, exc)
            return None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _extract_distinctive_snippet(line: str) -> str:
    """Extract a distinctive substring from a log line.

    Strips common timestamp/prefix patterns and returns the core message
    portion that can serve as a log_pattern for signature matching.
    """
    # Strip common syslog-style timestamps (e.g. "Mar 17 11:36:30")
    stripped = re.sub(
        r"^\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+\S+:\s*", "", line
    )
    # Strip ISO timestamps (e.g. "2026-03-17T11:36:30.123Z")
    stripped = re.sub(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s]*\s*", "", stripped)
    # Strip common log level prefixes
    stripped = re.sub(r"^(I|W|E|F)\d{4}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+\S+\]\s*", "", stripped)

    stripped = stripped.strip()
    if len(stripped) < 10:
        return ""
    # Cap at 200 chars to keep patterns manageable
    return stripped[:200]
