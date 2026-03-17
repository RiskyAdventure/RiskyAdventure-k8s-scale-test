"""Signature matching engine for the Known Issues Knowledge Base.

Computes similarity scores between Findings and KB entry signatures
using weighted event, log, and metric matching.
"""

import logging
import re
from typing import Dict, List, Optional, Tuple

from k8s_scale_test.models import K8sEvent, KBEntry, NodeDiagnostic, Signature

logger = logging.getLogger(__name__)

_DEFAULT_WEIGHTS: Dict[str, float] = {"event": 0.6, "log": 0.3, "metric": 0.1}


class SignatureMatcher:
    """Matches Findings against KB entry signatures using weighted scoring."""

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        threshold: float = 0.7,
    ) -> None:
        self.weights = weights if weights is not None else dict(_DEFAULT_WEIGHTS)
        self.threshold = threshold

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def score(
        self,
        finding_events: List[K8sEvent],
        finding_diagnostics: List[NodeDiagnostic],
        signature: Signature,
        evidence_references: Optional[List[str]] = None,
    ) -> float:
        """Compute a weighted similarity score in [0.0, 1.0].

        If the signature has empty event_reasons AND empty log_patterns the
        score is 0.0 (no match possible).
        """
        if not signature.event_reasons and not signature.log_patterns:
            return 0.0

        event_score = self._event_score(finding_events, signature)
        log_score = self._log_score(finding_diagnostics, signature)
        metric_score = self._metric_score(evidence_references or [], signature)

        total = (
            self.weights.get("event", 0.0) * event_score
            + self.weights.get("log", 0.0) * log_score
            + self.weights.get("metric", 0.0) * metric_score
        )
        # Clamp to [0.0, 1.0] for safety
        return max(0.0, min(1.0, total))

    # ------------------------------------------------------------------
    # Match
    # ------------------------------------------------------------------

    def match(
        self,
        finding_events: List[K8sEvent],
        finding_diagnostics: List[NodeDiagnostic],
        entries: List[KBEntry],
        evidence_references: Optional[List[str]] = None,
    ) -> List[Tuple[KBEntry, float]]:
        """Return active entries whose score exceeds the threshold, sorted descending."""
        results: List[Tuple[KBEntry, float]] = []
        for entry in entries:
            if entry.status != "active":
                continue
            s = self.score(
                finding_events, finding_diagnostics, entry.signature, evidence_references
            )
            if s >= self.threshold:
                results.append((entry, s))
        results.sort(key=lambda pair: pair[1], reverse=True)
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _event_score(events: List[K8sEvent], signature: Signature) -> float:
        """Fraction of signature event_reasons found in finding event reasons."""
        if not signature.event_reasons:
            return 0.0
        finding_reasons = {e.reason for e in events}
        matched = sum(1 for r in signature.event_reasons if r in finding_reasons)
        return matched / len(signature.event_reasons)

    @staticmethod
    def _log_score(diagnostics: List[NodeDiagnostic], signature: Signature) -> float:
        """Fraction of signature log_patterns matching any SSM output line."""
        if not signature.log_patterns:
            return 0.0

        # Collect all SSM output lines from every diagnostic
        all_lines: List[str] = []
        for diag in diagnostics:
            for ssm_result in (
                diag.kubelet_logs,
                diag.containerd_logs,
                diag.journal_kubelet,
                diag.journal_containerd,
                diag.resource_utilization,
            ):
                if ssm_result and ssm_result.output:
                    all_lines.extend(ssm_result.output.splitlines())

        matched = 0
        for pattern in signature.log_patterns:
            try:
                if any(re.search(pattern, line) for line in all_lines):
                    matched += 1
            except re.error:
                logger.warning("Invalid regex in log_patterns: %r", pattern)
                # Treat as non-matching
        return matched / len(signature.log_patterns)

    @staticmethod
    def _metric_score(
        evidence_references: List[str], signature: Signature
    ) -> float:
        """Fraction of signature metric_conditions found in evidence_references."""
        if not signature.metric_conditions:
            return 0.0
        matched = sum(
            1 for cond in signature.metric_conditions if cond in evidence_references
        )
        return matched / len(signature.metric_conditions)
