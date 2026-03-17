"""Node health sweep agent — samples fleet health at peak load.

Runs during hold-at-peak to answer: "we hit target pod count, but what
do the nodes look like under this load?" Uses 1 SSM call per sampled
node (single combined command) checking PSI, kubelet health, disk
readiness, and per-core CPU.

All SSM calls fire in parallel via asyncio.gather so wall time is ~15s
regardless of sample size. Raw output is persisted to the evidence store
for post-run inspection.
"""

from __future__ import annotations

import asyncio
import logging

from k8s_scale_test.diagnostics import NodeDiagnosticsCollector
from k8s_scale_test.evidence import EvidenceStore

log = logging.getLogger(__name__)

# Single combined command — 1 SSM call per node
HEALTH_CHECK_CMD = (
    "echo '===PSI_START==='; "
    "cat /proc/pressure/cpu 2>/dev/null; echo '===PSI_SEP==='; "
    "cat /proc/pressure/memory 2>/dev/null; echo '===PSI_SEP==='; "
    "cat /proc/pressure/io 2>/dev/null; "
    "echo '===KUBELET_START==='; "
    "systemctl is-active kubelet 2>&1; "
    "echo '===DISK_START==='; "
    "df -h / 2>/dev/null; "
    "echo '===MPSTAT_START==='; "
    "mpstat -P ALL 1 1 2>/dev/null | tail -n +4 || echo NO_MPSTAT"
)


class HealthSweepAgent:
    """Samples Ready nodes at peak load and checks for hidden issues."""

    def __init__(self, k8s_client, node_diag: NodeDiagnosticsCollector,
                 evidence_store: EvidenceStore, run_id: str) -> None:
        self.k8s_client = k8s_client
        self.node_diag = node_diag
        self.evidence_store = evidence_store
        self.run_id = run_id

    async def run(self, sample_size: int = 10) -> dict:
        """Sample Ready nodes and check health. Returns results dict."""
        log.info("Node health sweep: sampling %d nodes at peak load...", sample_size)
        results = {"nodes_sampled": 0, "healthy": 0, "ssm_failed": 0,
                   "issues": [], "node_details": []}
        try:
            loop = asyncio.get_event_loop()
            v1 = self.k8s_client.CoreV1Api()
            nodes = await loop.run_in_executor(None,
                lambda: v1.list_node(watch=False))

            ready_nodes = []
            for n in nodes.items:
                pid = n.spec.provider_id or ""
                iid = pid.rsplit("/", 1)[-1] if "/" in pid else ""
                if not iid:
                    continue
                for cond in (n.status.conditions or []):
                    if cond.type == "Ready" and cond.status == "True":
                        ready_nodes.append((n.metadata.name, iid))
                        break

            step = max(1, len(ready_nodes) // sample_size)
            sampled = ready_nodes[::step][:sample_size]
            results["nodes_sampled"] = len(sampled)

            async def _check_node(node_name, iid):
                try:
                    r = await self.node_diag.run_single_command(iid, HEALTH_CHECK_CMD)
                    if r.status != "Success" or not r.output:
                        return node_name, iid, r.status, "", []
                    issues = parse_sweep_output(node_name, r.output)
                    return node_name, iid, r.status, r.output, issues
                except Exception as exc:
                    log.debug("Sweep failed for %s: %s", node_name[:40], exc)
                    return node_name, iid, "Error", str(exc), []

            node_results = await asyncio.gather(
                *[_check_node(name, iid) for name, iid in sampled],
                return_exceptions=True,
            )

            for item in node_results:
                if isinstance(item, Exception):
                    continue
                node_name, iid, status, raw_output, node_issues = item
                results["node_details"].append({
                    "node_name": node_name, "instance_id": iid,
                    "status": status, "issues": node_issues,
                    "raw_output": raw_output,
                })
                if node_issues:
                    results["issues"].extend(node_issues)
                    log.warning("  %s: %s", node_name[:40], "; ".join(node_issues))
                elif status == "Success":
                    results["healthy"] += 1
                else:
                    results["ssm_failed"] += 1

            log.info("Node health sweep: %d/%d healthy, %d SSM failed, %d issues",
                     results["healthy"], results["nodes_sampled"],
                     results["ssm_failed"], len(results["issues"]))

            # Persist raw sweep data
            sweep_path = (self.evidence_store._run_dir(self.run_id)
                          / "diagnostics" / "health_sweep.json")
            self.evidence_store._write_json(sweep_path, results)
            log.info("Health sweep data saved to %s", sweep_path)

        except Exception as exc:
            log.error("Node health sweep failed: %s", exc)
        return results


# ---------------------------------------------------------------------------
# Output parser — module-level so it can be tested independently
# ---------------------------------------------------------------------------

def parse_sweep_output(node_name: str, output: str) -> list[str]:
    """Parse combined sweep SSM output into issue strings."""
    issues = []

    # PSI
    if "===PSI_START===" in output:
        psi = output.split("===PSI_START===", 1)[1]
        if "===KUBELET_START===" in psi:
            psi = psi.split("===KUBELET_START===", 1)[0]
        if "===PSI_SEP===" in psi:
            for i, section in enumerate(psi.split("===PSI_SEP===")):
                for line in section.strip().split("\n"):
                    if line.startswith("some "):
                        for part in line.split():
                            if part.startswith("avg10="):
                                try:
                                    v = float(part.split("=", 1)[1])
                                except ValueError:
                                    break
                                if i == 0 and v > 25.0:
                                    issues.append(f"CPU pressure avg10={v:.1f}% on {node_name}")
                                elif i == 1 and v > 25.0:
                                    issues.append(f"Memory pressure avg10={v:.1f}% on {node_name}")
                                elif i == 2 and v > 10.0:
                                    issues.append(f"IO pressure avg10={v:.1f}% on {node_name}")
                                break

    # Kubelet — systemctl check (API-side health comes from node conditions)
    if "===KUBELET_START===" in output:
        kub = output.split("===KUBELET_START===", 1)[1]
        if "===DISK_START===" in kub:
            kub = kub.split("===DISK_START===", 1)[0]
        systemctl = kub.strip()
        if systemctl and systemctl != "active":
            issues.append(f"Kubelet not active on {node_name}: {systemctl}")

    # Disk
    if "===DISK_START===" in output:
        disk = output.split("===DISK_START===", 1)[1]
        if "===MPSTAT_START===" in disk:
            disk = disk.split("===MPSTAT_START===", 1)[0]
        for line in disk.split("\n"):
            parts = line.split()
            if len(parts) >= 6 and parts[-1] == "/":
                if parts[1] in ("0", "0B", "0K"):
                    issues.append(f"Root filesystem 0 capacity on {node_name}")
                    break

    # mpstat
    if "===MPSTAT_START===" in output:
        mpstat = output.split("===MPSTAT_START===", 1)[1]
        saturated = []
        for line in mpstat.split("\n"):
            fields = line.split()
            if len(fields) < 11:
                continue
            cpu_idx = 2 if len(fields) > 1 and fields[1] in ("AM", "PM") else 1
            if cpu_idx >= len(fields):
                continue
            core_str = fields[cpu_idx]
            if core_str in ("all", "CPU"):
                continue
            try:
                core_id = int(core_str)
                if float(fields[-1]) < 10.0:
                    saturated.append(core_id)
            except (ValueError, IndexError):
                continue
        if saturated:
            cores = ",".join(str(c) for c in saturated[:5])
            issues.append(f"CPU cores saturated on {node_name}: [{cores}]"
                          + (f" +{len(saturated)-5} more" if len(saturated) > 5 else ""))

    return issues
