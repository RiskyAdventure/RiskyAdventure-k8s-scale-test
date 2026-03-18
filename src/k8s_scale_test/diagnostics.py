"""Node diagnostics collection via AWS SSM.

Batched: sends all commands to all nodes at once, then polls all results
in parallel. Cuts collection time from O(nodes * commands * poll_time)
to O(poll_time) — typically 10-15s regardless of node/command count.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from k8s_scale_test.evidence import EvidenceStore
from k8s_scale_test.models import NodeDiagnostic, SSMCommandResult, TestConfig

log = logging.getLogger(__name__)


def resolve_instance_id(provider_id: str) -> str:
    if "/" in provider_id:
        return provider_id.rsplit("/", 1)[-1]
    return provider_id


class NodeDiagnosticsCollector:
    def __init__(self, config: TestConfig, aws_client,
                 evidence_store: EvidenceStore, run_id: str) -> None:
        self.config = config
        self.aws_client = aws_client
        self.evidence_store = evidence_store
        self.run_id = run_id
        self._ssm = None

    def _get_ssm(self):
        if self._ssm is None:
            self._ssm = (self.aws_client.client("ssm")
                         if hasattr(self.aws_client, "client")
                         else self.aws_client)
        return self._ssm

    async def collect(self, node_name: str, instance_id: str,
                      extra_commands: dict[str, str] | None = None) -> NodeDiagnostic:
        """Run all SSM commands on a node. All boto3 calls run in thread executor
        so they don't block the async event loop (keeps the monitor polling)."""
        lines = self.config.ssm_log_lines
        mins = self.config.ssm_journal_minutes
        loop = asyncio.get_event_loop()

        all_commands = {
            "kubelet_logs": f"journalctl -u kubelet --no-pager -n {lines}",
            "containerd_logs": f"journalctl -u containerd --no-pager -n {lines}",
            "journal_kubelet": f"journalctl -u kubelet --no-pager --since '{mins} minutes ago'",
            "journal_containerd": f"journalctl -u containerd --no-pager --since '{mins} minutes ago'",
            "resource_utilization": "top -bn1 | head -20; echo '---'; df -h; echo '---'; free -h",
        }
        if extra_commands:
            all_commands.update(extra_commands)

        ssm = self._get_ssm()

        # Journal commands on heavily loaded nodes (stress-ng, freshly
        # provisioned) can take >30s. Use 90s for journals, 30s otherwise.
        _JOURNAL_KEYS = {"kubelet_logs", "containerd_logs", "journal_kubelet", "journal_containerd"}
        pending = {}
        for key, cmd in all_commands.items():
            timeout = 90 if key in _JOURNAL_KEYS else 30
            try:
                resp = await loop.run_in_executor(None, lambda c=cmd, t=timeout: ssm.send_command(
                    InstanceIds=[instance_id],
                    DocumentName="AWS-RunShellScript",
                    Parameters={"commands": [c]},
                    TimeoutSeconds=t,
                ))
                pending[key] = (resp["Command"]["CommandId"], cmd)
            except Exception as exc:
                pending[key] = (None, cmd)
                log.error("SSM send failed [%s] on %s: %s", key, instance_id, exc)

        # Poll all until done — get_command_invocation in executor
        results: dict[str, SSMCommandResult] = {}
        for _ in range(90):
            if not pending:
                break
            await asyncio.sleep(1)
            done_keys = []
            for key, (cmd_id, cmd_str) in pending.items():
                if cmd_id is None:
                    results[key] = SSMCommandResult(
                        instance_id=instance_id, command=cmd_str,
                        command_id="", status="Failed", output="",
                        error="send_command failed")
                    done_keys.append(key)
                    continue
                try:
                    inv = await loop.run_in_executor(None, lambda cid=cmd_id: ssm.get_command_invocation(
                        CommandId=cid, InstanceId=instance_id))
                    if inv["Status"] in ("Success", "Failed", "TimedOut", "Cancelled"):
                        results[key] = SSMCommandResult(
                            instance_id=instance_id, command=cmd_str,
                            command_id=cmd_id, status=inv["Status"],
                            output=inv.get("StandardOutputContent", ""),
                            error=inv.get("StandardErrorContent", ""))
                        done_keys.append(key)
                except Exception:
                    pass
            for k in done_keys:
                del pending[k]

        for key, (cmd_id, cmd_str) in pending.items():
            results[key] = SSMCommandResult(
                instance_id=instance_id, command=cmd_str,
                command_id=cmd_id or "", status="TimedOut",
                output="", error="SSM polling timed out after 90s")

        # Merge extra output into resource_utilization
        if extra_commands:
            extra_output = []
            for key in extra_commands:
                r = results.get(key)
                if r and r.output:
                    extra_output.append(f"\n=== {key} ===\n{r.output}")
            if extra_output and "resource_utilization" in results:
                base = results["resource_utilization"]
                results["resource_utilization"] = SSMCommandResult(
                    instance_id=base.instance_id, command=base.command,
                    command_id=base.command_id, status=base.status,
                    output=base.output + "".join(extra_output),
                    error=base.error)

        empty = SSMCommandResult(instance_id=instance_id, command="",
                                 command_id="", status="Skipped", output="", error="")
        diagnostic = NodeDiagnostic(
            node_name=node_name, instance_id=instance_id,
            collection_timestamp=datetime.now(timezone.utc),
            kubelet_logs=results.get("kubelet_logs", empty),
            containerd_logs=results.get("containerd_logs", empty),
            journal_kubelet=results.get("journal_kubelet", empty),
            journal_containerd=results.get("journal_containerd", empty),
            resource_utilization=results.get("resource_utilization", empty),
        )
        self.evidence_store.save_node_diagnostic(self.run_id, diagnostic)
        log.info("  SSM collected %d results from %s", len(results), node_name[:40])
        return diagnostic

    # Keep the old single-command method for the CNI analyzer's IPAMD log collection
    async def run_single_command(self, instance_id: str, command: str) -> SSMCommandResult:
        """Run one SSM command and wait for result.

        All boto3 calls run in thread executor so they don't block the
        async event loop.
        """
        loop = asyncio.get_event_loop()
        ssm = self._get_ssm()

        def _execute():
            """Synchronous SSM send + poll — runs entirely in thread."""
            import time
            try:
                resp = ssm.send_command(
                    InstanceIds=[instance_id],
                    DocumentName="AWS-RunShellScript",
                    Parameters={"commands": [command]},
                    TimeoutSeconds=30,
                )
                cmd_id = resp["Command"]["CommandId"]
                for _ in range(30):
                    time.sleep(1)
                    inv = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
                    if inv["Status"] in ("Success", "Failed", "TimedOut", "Cancelled"):
                        return SSMCommandResult(
                            instance_id=instance_id, command=command,
                            command_id=cmd_id, status=inv["Status"],
                            output=inv.get("StandardOutputContent", ""),
                            error=inv.get("StandardErrorContent", ""))
                return SSMCommandResult(instance_id=instance_id, command=command,
                                        command_id=cmd_id, status="TimedOut", output="",
                                        error="Timed out after 30s")
            except Exception as exc:
                return SSMCommandResult(instance_id=instance_id, command=command,
                                        command_id="", status="Failed", output="",
                                        error=str(exc))

        return await loop.run_in_executor(None, _execute)
