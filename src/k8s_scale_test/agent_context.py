"""Context file writer for the AI sub-agent integration.

Writes and updates agent_context.json in the evidence store run directory.
All writes are synchronous JSON dumps to local disk (<10ms).
The context file is the sole interface from Python tool -> Kiro agent.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from k8s_scale_test.evidence import EvidenceStore
from k8s_scale_test.models import Alert, Finding, TestConfig

log = logging.getLogger(__name__)


class ContextFileWriter:
    """Writes agent_context.json to the evidence store run directory.

    All writes are synchronous JSON dumps to local disk (<10ms).
    The context file is the sole interface from Python tool -> Kiro agent.
    """

    def __init__(self, evidence_store: EvidenceStore, run_id: str, config: TestConfig) -> None:
        self._store = evidence_store
        self._run_id = run_id
        self._target_pods = config.target_pods
        self._amp_workspace_id = config.amp_workspace_id
        self._cloudwatch_log_group = config.cloudwatch_log_group
        self._eks_cluster_name = config.eks_cluster_name
        self._evidence_dir = str(evidence_store._run_dir(run_id))

    def write_initial_context(self, namespaces: list[str], node_list: list[str]) -> None:
        """Write the initial context file when monitoring starts."""
        try:
            now = datetime.now(timezone.utc)
            context: dict = {
                "run_id": self._run_id,
                "test_start": now.isoformat(),
                "target_pods": self._target_pods,
                "namespaces": namespaces,
                "current_phase": "initializing",
                "phase_start": now.isoformat(),
            }
            # Omit None observability fields per Requirement 2.6
            if self._amp_workspace_id is not None:
                context["amp_workspace_id"] = self._amp_workspace_id
            if self._cloudwatch_log_group is not None:
                context["cloudwatch_log_group"] = self._cloudwatch_log_group
            if self._eks_cluster_name is not None:
                context["eks_cluster_name"] = self._eks_cluster_name

            context["alerts"] = []
            context["finding_summaries"] = []
            context["evidence_dir"] = self._evidence_dir

            self._store.write_agent_context(self._run_id, context)
        except Exception as exc:
            log.warning("Failed to write initial agent context: %s", exc)

    def update_phase(self, phase: str, timestamp: datetime) -> None:
        """Update the current test phase and its start timestamp."""
        try:
            context = self._store.load_agent_context(self._run_id)
            if context is None:
                log.warning("Cannot update phase: agent context file not found")
                return
            context["current_phase"] = phase
            context["phase_start"] = timestamp.isoformat()
            self._store.write_agent_context(self._run_id, context)
        except Exception as exc:
            log.warning("Failed to update agent context phase: %s", exc)

    def append_alert(self, alert: Alert) -> None:
        """Append a rate drop alert to the alerts list in the context file."""
        try:
            context = self._store.load_agent_context(self._run_id)
            if context is None:
                log.warning("Cannot append alert: agent context file not found")
                return
            alert_dict = {
                "timestamp": alert.timestamp.isoformat(),
                "alert_type": alert.alert_type.value if hasattr(alert.alert_type, "value") else str(alert.alert_type),
                "message": alert.message,
                "current_rate": alert.context.get("current_rate"),
                "rolling_avg": alert.context.get("rolling_avg"),
                "ready_count": alert.context.get("ready_count"),
                "pending_count": alert.context.get("pending_count"),
            }
            context["alerts"].append(alert_dict)
            self._store.write_agent_context(self._run_id, context)
        except Exception as exc:
            log.warning("Failed to append alert to agent context: %s", exc)

    def append_finding_summary(self, finding: Finding) -> None:
        """Append a finding summary to the finding_summaries list in the context file."""
        try:
            context = self._store.load_agent_context(self._run_id)
            if context is None:
                log.warning("Cannot append finding summary: agent context file not found")
                return
            summary_dict = {
                "finding_id": finding.finding_id,
                "severity": finding.severity.value if hasattr(finding.severity, "value") else str(finding.severity),
                "symptom": finding.symptom,
                "affected_resources": finding.affected_resources[:10],
            }
            context["finding_summaries"].append(summary_dict)
            self._store.write_agent_context(self._run_id, context)
        except Exception as exc:
            log.warning("Failed to append finding summary to agent context: %s", exc)
