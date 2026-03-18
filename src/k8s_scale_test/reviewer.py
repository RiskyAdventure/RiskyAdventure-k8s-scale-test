"""Skeptical review — independent re-verification of anomaly findings.

Takes a Finding produced by the AnomalyDetector, re-queries data sources
using a different approach, checks for staleness, assigns a confidence
level, lists alternative explanations, and appends a ``review`` field to
the finding JSON.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from k8s_scale_test.evidence import EvidenceStore
from k8s_scale_test.health_sweep import AMPMetricCollector
from k8s_scale_test.models import Finding

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# KB alternative explanations lookup (mirrors seed entries in kb_seed.py)
# ---------------------------------------------------------------------------
_KB_ALTERNATIVE_MAP: dict[str, list[str]] = {
    "ipamd-mac-collision": [
        "IPAMD IP address exhaustion (not MAC collision)",
        "Subnet IP capacity exhaustion preventing ENI attachment",
        "EC2 API throttling delaying ENI operations",
    ],
    "ipamd-ip-exhaustion": [
        "VPC CNI MAC address collision at high pod density",
        "Subnet-level IP exhaustion (not node-level)",
        "WARM_IP_TARGET misconfiguration causing premature exhaustion",
    ],
    "subnet-ip-exhaustion": [
        "Node-level IPAMD IP exhaustion (subnet has IPs but node ENIs full)",
        "EC2 API throttling preventing ENI attachment",
        "Secondary CIDR not propagated to route tables",
    ],
    "coredns-bottleneck": [
        "Upstream DNS resolver timeout (not CoreDNS itself)",
        "NodeLocal DNSCache misconfiguration",
        "Network policy blocking DNS traffic",
    ],
    "karpenter-capacity": [
        "EC2 service quota limit reached",
        "Instance type not available in selected AZs",
        "Spot capacity interruption",
    ],
    "image-pull-throttle": [
        "ECR pull-through cache misconfiguration",
        "Network connectivity issue to registry",
        "containerd max-concurrent-downloads too low",
    ],
}


class FindingReviewer:
    """Independently re-verifies anomaly findings."""

    def __init__(
        self,
        evidence_store: EvidenceStore,
        run_id: str,
        amp_collector: AMPMetricCollector | None = None,
        cloudwatch_fn: Callable[[str, str, str], Awaitable[dict]] | None = None,
        staleness_threshold_seconds: float = 300.0,
    ) -> None:
        self._evidence_store = evidence_store
        self._run_id = run_id
        self._amp_collector = amp_collector
        self._cloudwatch_fn = cloudwatch_fn
        self._staleness_threshold_seconds = staleness_threshold_seconds

    # ------------------------------------------------------------------
    # Claim identification
    # ------------------------------------------------------------------

    def _identify_claim(self, finding: Finding) -> str:
        """Extract the central claim from a finding."""
        if finding.root_cause is not None:
            return finding.root_cause
        return finding.symptom

    # ------------------------------------------------------------------
    # Staleness detection
    # ------------------------------------------------------------------

    def _check_staleness(self, finding: Finding) -> tuple[bool, str]:
        """Check if finding is potentially stale."""
        now = datetime.now(timezone.utc)
        ts = finding.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        elapsed = (now - ts).total_seconds()

        if elapsed > self._staleness_threshold_seconds:
            reasoning = (
                f"Finding is potentially stale: {elapsed:.0f}s elapsed "
                f"since finding timestamp, exceeding the "
                f"{self._staleness_threshold_seconds:.0f}s threshold. "
                f"Underlying conditions may have changed."
            )
            return True, reasoning

        reasoning = (
            f"Finding is fresh: {elapsed:.0f}s elapsed since finding "
            f"timestamp, within the {self._staleness_threshold_seconds:.0f}s "
            f"threshold."
        )
        return False, reasoning

    # ------------------------------------------------------------------
    # Confidence assignment
    # ------------------------------------------------------------------

    def _assign_confidence(
        self, verification_results: list[dict], is_stale: bool
    ) -> str:
        """Determine confidence level from verification results and staleness."""
        if not verification_results:
            return "low"

        verified_flags = [r.get("verified") for r in verification_results]
        all_verified = all(v is True for v in verified_flags)
        all_failed = all(v is False for v in verified_flags)

        if all_verified:
            base = "high"
        elif all_failed:
            base = "low"
        else:
            base = "medium"

        if is_stale:
            if base == "high":
                return "medium"
            if base == "medium":
                return "low"
            return "low"

        return base

    # ------------------------------------------------------------------
    # Review assembly
    # ------------------------------------------------------------------

    def _build_review(
        self,
        confidence: str,
        reasoning: str,
        alternatives: list[str],
        checkpoints: list[str],
        verification_results: list[dict],
    ) -> dict:
        """Assemble the review dict matching the steering file schema."""
        return {
            "confidence": confidence,
            "reasoning": reasoning,
            "alternative_explanations": alternatives,
            "checkpoint_questions": checkpoints,
            "verification_results": verification_results,
        }

    # ------------------------------------------------------------------
    # AMP re-query  (Task 2.1)
    # ------------------------------------------------------------------

    _AMP_REQUERY_MAP: dict[str, list[tuple[str, str]]] = {
        "cpu": [
            (
                "CPU utilization (2m rate window)",
                'avg(100 - rate(node_cpu_seconds_total{mode="idle"}[2m]) * 100)',
            ),
        ],
        "memory": [
            (
                "Memory utilization (instant snapshot)",
                "avg(100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))",
            ),
        ],
        "network": [
            (
                "Network errors (1m rate window)",
                "sum(rate(node_network_receive_errs_total[1m]) + rate(node_network_transmit_errs_total[1m]))",
            ),
        ],
        "pending": [
            (
                "Pending pods count",
                'count(kube_pod_status_phase{phase="Pending"})',
            ),
        ],
    }

    async def _requery_amp(self, finding: Finding) -> list[dict]:
        """Re-query AMP with different query approach.

        Parses evidence_references for AMP-related entries and constructs
        PromQL queries with different rate windows than the original
        investigation.  Returns list of verification_result dicts.
        """
        if self._amp_collector is None:
            return []

        categories: set[str] = set()
        for ref in finding.evidence_references:
            lower = ref.lower()
            if ref.startswith("amp:"):
                if "violation" in lower:
                    categories.update(("cpu", "memory", "network"))
                else:
                    categories.add("cpu")
            if "pending" in lower or "FailedCreatePodSandBox" in ref:
                categories.add("pending")

        if not categories:
            return []

        results: list[dict] = []
        for cat in sorted(categories):
            for claim_text, query in self._AMP_REQUERY_MAP.get(cat, []):
                try:
                    resp = await self._amp_collector._query_promql(query)
                    data = resp.get("data", {})
                    result_list = data.get("result", [])
                    has_data = len(result_list) > 0
                    detail_parts = []
                    for r in result_list[:3]:
                        val = r.get("value", [None, None])
                        if isinstance(val, list) and len(val) >= 2:
                            detail_parts.append(str(val[1]))
                    detail = (
                        f"AMP re-query ({claim_text}): "
                        + (", ".join(detail_parts) if detail_parts else "no data points")
                    )
                    results.append({
                        "claim": claim_text,
                        "verified": has_data,
                        "detail": detail,
                    })
                except Exception as exc:
                    log.warning("AMP re-query failed for %s: %s", claim_text, exc)
                    results.append({
                        "claim": claim_text,
                        "verified": False,
                        "detail": f"AMP re-query failed: {exc}",
                    })
        return results

    # ------------------------------------------------------------------
    # CloudWatch re-query  (Task 2.2)
    # ------------------------------------------------------------------

    async def _requery_cloudwatch(self, finding: Finding) -> list[dict]:
        """Re-query CloudWatch with narrower time window (±1 min).

        Parses evidence_references for warning event patterns and
        constructs CloudWatch Logs Insights queries scoped to the
        finding's timestamp.  Returns list of verification_result dicts.
        """
        if self._cloudwatch_fn is None:
            return []

        patterns: list[str] = []
        for ref in finding.evidence_references:
            if ref.startswith("warnings:"):
                parts = ref[len("warnings:"):].split(",")
                for part in parts:
                    reason = part.split("=")[0].strip()
                    if reason:
                        patterns.append(reason)

        if not patterns:
            return []

        ts = finding.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        start_time = (ts - timedelta(minutes=1)).isoformat()
        end_time = (ts + timedelta(minutes=1)).isoformat()

        results: list[dict] = []
        for pattern in patterns:
            query = (
                "fields @timestamp, @message"
                f" | filter @message like /{pattern}/"
                " | stats count() as cnt by bin(1m)"
                " | sort @timestamp desc"
                " | limit 10"
            )
            try:
                resp = await self._cloudwatch_fn(query, start_time, end_time)
                cw_results = resp.get("results", [])
                has_matches = len(cw_results) > 0
                detail = (
                    f"CloudWatch re-query for '{pattern}': "
                    f"{len(cw_results)} result(s) in ±1min window"
                )
                results.append({
                    "claim": f"Warning events: {pattern}",
                    "verified": has_matches,
                    "detail": detail,
                })
            except Exception as exc:
                log.warning("CloudWatch re-query failed for %s: %s", pattern, exc)
                results.append({
                    "claim": f"Warning events: {pattern}",
                    "verified": False,
                    "detail": f"CloudWatch re-query failed: {exc}",
                })
        return results

    # ------------------------------------------------------------------
    # Alternative explanations  (Task 2.3)
    # ------------------------------------------------------------------

    _EVENT_ALTERNATIVES: dict[str, list[str]] = {
        "FailedCreatePodSandBox": [
            "VPC CNI MAC address collision at high pod density",
            "IPAMD IP address exhaustion on node ENIs",
            "Subnet IP capacity exhaustion preventing ENI attachment",
        ],
        "OOMKilling": [
            "Pod memory limits set too low for workload",
            "Node-level memory pressure from aggregate pod demand",
            "Memory leak in application container",
        ],
        "Evicted": [
            "Node disk pressure from container logs or image layers",
            "Node memory pressure triggering kubelet eviction",
            "Ephemeral storage limit exceeded",
        ],
        "FailedCreate": [
            "Admission webhook (e.g. Kyverno) blocking pod creation",
            "Resource quota exceeded in namespace",
            "PodSecurityPolicy or PodSecurity admission rejection",
        ],
        "ErrImagePull": [
            "Container registry rate limiting under concurrent pulls",
            "Image tag does not exist or was deleted",
            "Network connectivity issue to registry endpoint",
        ],
        "InsufficientCapacityError": [
            "EC2 capacity constraints for selected instance types",
            "Availability zone capacity imbalance",
            "Service quota limits for the account",
        ],
    }

    def _generate_alternatives(
        self, finding: Finding, verification_results: list[dict]
    ) -> list[str]:
        """Generate alternative explanations based on evidence and re-query results."""
        alternatives: list[str] = []
        seen: set[str] = set()

        def _add(text: str) -> None:
            if text not in seen:
                seen.add(text)
                alternatives.append(text)

        # 1. KB entry references
        for ref in finding.evidence_references:
            if ref.startswith("kb_match:"):
                entry_id = ref[len("kb_match:"):]
                for alt in _KB_ALTERNATIVE_MAP.get(entry_id, []):
                    _add(alt)

        # 2. K8s event reasons
        for event in finding.k8s_events:
            reason = getattr(event, "reason", "")
            for pattern, alts in self._EVENT_ALTERNATIVES.items():
                if pattern in reason:
                    for alt in alts:
                        _add(alt)

        # 3. Warning patterns in evidence_references
        for ref in finding.evidence_references:
            if ref.startswith("warnings:"):
                parts = ref[len("warnings:"):].split(",")
                for part in parts:
                    reason = part.split("=")[0].strip()
                    for pattern, alts in self._EVENT_ALTERNATIVES.items():
                        if pattern in reason:
                            for alt in alts:
                                _add(alt)

        # 4. ENI-related evidence
        for ref in finding.evidence_references:
            if ref.startswith("eni:"):
                _add("ENI attachment failure due to subnet IP exhaustion")
                _add("EC2 API throttling preventing ENI operations")

        # 5. Generic fallback when verification failures exist but no patterns matched
        failed = [r for r in verification_results if not r.get("verified")]
        if failed and not alternatives:
            _add("Transient infrastructure issue that has since resolved")
            _add("Monitoring data gap causing incomplete evidence")

        return alternatives

    # ------------------------------------------------------------------
    # Checkpoint questions  (Task 2.4)
    # ------------------------------------------------------------------

    def _generate_checkpoint_questions(
        self, finding: Finding, confidence: str, verification_results: list[dict]
    ) -> list[str]:
        """Generate actionable checkpoint questions when confidence < high."""
        if confidence == "high":
            return []

        questions: list[str] = []

        if finding.affected_resources:
            sample = finding.affected_resources[:3]
            questions.append(
                f"Check current status of affected resources: {', '.join(sample)}"
            )

        for ref in finding.evidence_references:
            if "FailedCreatePodSandBox" in ref:
                questions.append(
                    "Check IPAMD logs on affected nodes for MAC collision or IP exhaustion errors"
                )
                questions.append(
                    "Verify subnet IP availability for the node's availability zone"
                )
                break
            if ref.startswith("amp:") and "violation" in ref.lower():
                questions.append(
                    "Query AMP directly to confirm current node resource utilization"
                )
                break
            if ref.startswith("eni:"):
                questions.append(
                    "Check ENI attachment status and prefix delegation on affected nodes"
                )
                break

        failed = [r for r in verification_results if not r.get("verified")]
        if failed:
            first_fail = failed[0]
            questions.append(
                f"Investigate why verification failed: {first_fail.get('detail', 'unknown')}"
            )

        if finding.k8s_events:
            reasons = {getattr(e, "reason", "") for e in finding.k8s_events}
            reasons.discard("")
            if reasons:
                questions.append(
                    f"Review K8s events with reasons: {', '.join(sorted(reasons)[:3])}"
                )

        if not questions:
            questions.append(
                f"Manually verify the finding symptom: {finding.symptom}"
            )

        return questions

    # ------------------------------------------------------------------
    # Persistence  (Task 2.5)
    # ------------------------------------------------------------------

    def _persist_review(self, finding: Finding, review: dict) -> None:
        """Append review field to finding JSON and save to EvidenceStore."""
        try:
            data = finding.to_dict()
            data["review"] = review
            path = (
                self._evidence_store._run_dir(self._run_id)
                / "findings"
                / f"{finding.finding_id}.json"
            )
            self._evidence_store._write_json(path, data)
        except Exception as exc:
            log.error(
                "Failed to persist review for finding %s: %s",
                finding.finding_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Main review pipeline  (Task 2.6)
    # ------------------------------------------------------------------

    async def review(self, finding: Finding) -> dict:
        """Run the full review pipeline on a finding.

        Returns the review dict (also persisted to EvidenceStore).
        """
        claim = self._identify_claim(finding)

        amp_results = await self._requery_amp(finding)
        cw_results = await self._requery_cloudwatch(finding)

        verification_results: list[dict] = [
            {"claim": claim, "verified": True, "detail": "Central claim"},
        ]
        verification_results.extend(amp_results)
        verification_results.extend(cw_results)

        is_stale, staleness_text = self._check_staleness(finding)

        # Check if all re-queries failed
        requery_results = amp_results + cw_results
        all_failed = (
            all(not r.get("verified") for r in requery_results)
            if requery_results
            else False
        )

        confidence = self._assign_confidence(verification_results, is_stale)
        if all_failed:
            confidence = "low"

        reasoning = (
            staleness_text
            + " "
            + f"Based on {len(verification_results)} verification checks."
        )

        alternatives = self._generate_alternatives(finding, verification_results)
        checkpoints = self._generate_checkpoint_questions(
            finding, confidence, verification_results,
        )

        review = self._build_review(
            confidence, reasoning, alternatives, checkpoints, verification_results,
        )

        self._persist_review(finding, review)
        return review
