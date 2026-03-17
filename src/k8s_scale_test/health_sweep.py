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
import json
import logging
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass

from k8s_scale_test.diagnostics import NodeDiagnosticsCollector
from k8s_scale_test.evidence import EvidenceStore
from k8s_scale_test.models import TestConfig

log = logging.getLogger(__name__)


@dataclass
class NodeMetricResult:
    node_name: str
    metric_category: str  # "cpu", "memory", "disk", "network_errors", "pod_restarts"
    value: float


@dataclass
class NodeConditionResult:
    node_name: str
    issues: list[str]  # e.g. ["NotReady: KubeletNotReady", "MemoryPressure: MemoryPressureExists"]


# Threshold definitions: (threshold, format_string)
# Issue is raised when value is strictly greater than the threshold.
_THRESHOLDS: dict[str, tuple[float, str]] = {
    "cpu": (90.0, "High CPU utilization {value:.1f}% on {node}"),
    "memory": (90.0, "High memory usage {value:.1f}% on {node}"),
    "disk": (85.0, "High disk usage {value:.1f}% on {node}"),
    "network_errors": (0.0, "Network errors {value:.1f}/s on {node}"),
    "pod_restarts": (5.0, "Pod restarts {value:.0f} in 15m on {node}"),
}


def check_threshold(result: NodeMetricResult) -> str | None:
    """Return an issue string if *result* exceeds its category threshold, else None."""
    entry = _THRESHOLDS.get(result.metric_category)
    if entry is None:
        return None
    threshold, fmt = entry
    if result.value > threshold:
        return fmt.format(value=result.value, node=result.node_name)
    return None

def parse_promql_response(response: dict, category: str) -> list[NodeMetricResult]:
    """Parse a Prometheus instant-query vector response into NodeMetricResult list.

    Returns an empty list when the response is malformed or has a non-success
    status.  Individual result entries that are malformed are silently skipped
    rather than failing the whole parse.
    """
    try:
        if not isinstance(response, dict):
            return []
        if response.get("status") != "success":
            return []
        data = response.get("data")
        if not isinstance(data, dict):
            return []
        if data.get("resultType") != "vector":
            return []
        results = data.get("result")
        if not isinstance(results, list):
            return []
    except Exception:
        return []

    parsed: list[NodeMetricResult] = []
    for entry in results:
        try:
            if not isinstance(entry, dict):
                continue
            metric = entry.get("metric")
            if not isinstance(metric, dict):
                continue
            node_name = metric.get("node") or metric.get("instance")
            if not node_name or not isinstance(node_name, str):
                continue
            value_pair = entry.get("value")
            if not isinstance(value_pair, (list, tuple)) or len(value_pair) < 2:
                continue
            value = float(value_pair[1])
            parsed.append(NodeMetricResult(node_name, category, value))
        except (TypeError, ValueError, IndexError):
            continue
    return parsed

class K8sConditionChecker:
    """Checks node conditions via the Kubernetes API."""

    CONDITION_TYPES = ("Ready", "DiskPressure", "MemoryPressure", "PIDPressure")

    def __init__(self, k8s_client) -> None:
        self.k8s_client = k8s_client

    async def check_all(self) -> list[NodeConditionResult]:
        """Query all node conditions in one API call. Returns per-node results.

        Only nodes with at least one issue are included in the output.
        Returns an empty list on K8s API failure (logs error, does not raise).
        """
        try:
            loop = asyncio.get_event_loop()
            v1 = self.k8s_client.CoreV1Api()
            nodes = await loop.run_in_executor(
                None, lambda: v1.list_node(watch=False)
            )
        except Exception as exc:
            log.error("K8s API node condition query failed: %s", exc)
            return []

        results: list[NodeConditionResult] = []
        for node in nodes.items:
            node_name = node.metadata.name
            issues: list[str] = []
            for cond in node.status.conditions or []:
                if cond.type not in self.CONDITION_TYPES:
                    continue
                if cond.type == "Ready" and cond.status != "True":
                    reason = cond.reason or "Unknown"
                    issues.append(f"NotReady: {reason}")
                elif cond.type in ("DiskPressure", "MemoryPressure", "PIDPressure") and cond.status == "True":
                    reason = cond.reason or "Unknown"
                    issues.append(f"{cond.type}: {reason}")
            if issues:
                results.append(NodeConditionResult(node_name, issues))
        return results


class AMPMetricCollector:
    """Collects node metrics from AMP or direct Prometheus via PromQL."""

    _QUERIES: dict[str, str] = {
        "cpu": '100 - (avg by(node)(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
        "memory": "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
        "disk": (
            '100 - (node_filesystem_avail_bytes{mountpoint="/",fstype!="tmpfs"}'
            ' / node_filesystem_size_bytes{mountpoint="/",fstype!="tmpfs"} * 100)'
        ),
        "network_errors": (
            "sum by(node)(rate(node_network_receive_errs_total[5m])"
            " + rate(node_network_transmit_errs_total[5m]))"
        ),
        "pod_restarts": "sum by(node)(increase(kube_pod_container_status_restarts_total[15m]))",
    }

    def __init__(
        self,
        amp_workspace_id: str | None,
        prometheus_url: str | None,
        aws_profile: str | None = None,
        region: str = "us-west-2",
    ) -> None:
        self._amp_workspace_id = amp_workspace_id
        self._prometheus_url = prometheus_url
        self._region = region
        self._use_sigv4 = amp_workspace_id is not None

        if amp_workspace_id:
            self._endpoint = (
                f"https://aps-workspaces.{region}.amazonaws.com"
                f"/workspaces/{amp_workspace_id}/api/v1/query"
            )
        elif prometheus_url:
            self._endpoint = f"{prometheus_url.rstrip('/')}/api/v1/query"
        else:
            raise ValueError("Either amp_workspace_id or prometheus_url must be provided")

        # Lazily resolved SigV4 credentials (only when AMP is used)
        self._credentials = None
        if self._use_sigv4:
            import botocore.session

            session = botocore.session.Session(profile=aws_profile)
            self._credentials = session.get_credentials()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def collect_all(self) -> dict[str, list[NodeMetricResult]]:
        """Run all PromQL queries concurrently. Returns dict keyed by metric category.

        Keys: "cpu", "memory", "disk", "network_errors", "pod_restarts"
        Values: list of NodeMetricResult per node

        Also stores raw responses in ``self.raw_responses`` for inclusion
        in the persisted evidence.
        """
        self.raw_responses: dict[str, dict] = {}

        async def _run_query(category: str, query: str):
            try:
                raw = await self._query_promql(query)
                self.raw_responses[category] = raw
                return category, parse_promql_response(raw, category)
            except Exception as exc:
                log.warning("PromQL query failed for %s: %s", category, exc)
                self.raw_responses[category] = {}
                return category, []

        tasks = [
            _run_query(cat, q) for cat, q in self._QUERIES.items()
        ]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        metrics: dict[str, list[NodeMetricResult]] = {}
        for item in results_list:
            if isinstance(item, Exception):
                log.warning("Unexpected error in PromQL gather: %s", item)
                continue
            category, parsed = item
            metrics[category] = parsed

        return metrics

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _query_promql(self, query: str) -> dict:
        """Execute a single PromQL instant query. Returns raw Prometheus JSON response."""
        params = urllib.parse.urlencode({"query": query})
        url = f"{self._endpoint}?{params}"

        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")

        if self._use_sigv4:
            self._sign_request(req, url)

        loop = asyncio.get_event_loop()
        response_body: bytes = await loop.run_in_executor(None, self._do_http, req)
        return json.loads(response_body)

    def _sign_request(self, req: urllib.request.Request, url: str) -> None:
        """Apply SigV4 signature headers to *req*."""
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        aws_req = AWSRequest(method="GET", url=url, headers={"Accept": "application/json"})
        SigV4Auth(self._credentials, "aps", self._region).add_auth(aws_req)

        for header, value in aws_req.headers.items():
            req.add_header(header, value)

    @staticmethod
    def _do_http(req: urllib.request.Request) -> bytes:
        """Blocking HTTP call — intended to run inside ``run_in_executor``."""
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()


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


class SSMFallbackCollector:
    """SSM-based sweep — used when AMP/Prometheus are not configured,
    or for collecting low-level data (PSI, bpftrace, BPF tools) not in AMP."""

    def __init__(self, k8s_client, node_diag: NodeDiagnosticsCollector,
                 extra_commands: dict[str, str] | None = None) -> None:
        self.k8s_client = k8s_client
        self.node_diag = node_diag
        self.extra_commands = extra_commands or {}

    async def collect(self, sample_size: int = 10) -> dict:
        """Sample Ready nodes via SSM. Returns partial Sweep_Result dict.

        Runs the standard health check command plus any extra_commands.
        Extra command output is stored in node_details[].extra_diagnostics.
        """
        results: dict = {
            "nodes_sampled": 0, "healthy": 0, "ssm_failed": 0,
            "issues": [], "node_details": [],
        }
        try:
            loop = asyncio.get_event_loop()
            v1 = self.k8s_client.CoreV1Api()
            nodes = await loop.run_in_executor(
                None, lambda: v1.list_node(watch=False)
            )

            ready_nodes: list[tuple[str, str]] = []
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

            async def _check_node(node_name: str, iid: str):
                try:
                    r = await self.node_diag.run_single_command(iid, HEALTH_CHECK_CMD)
                    if r.status != "Success" or not r.output:
                        return node_name, iid, r.status, "", [], {}
                    issues = parse_sweep_output(node_name, r.output)

                    # Run extra commands if configured
                    extra_diag: dict[str, str] = {}
                    for label, cmd in self.extra_commands.items():
                        try:
                            er = await self.node_diag.run_single_command(iid, cmd)
                            extra_diag[label] = er.output if er.status == "Success" else f"FAILED: {er.status}"
                        except Exception as exc:
                            extra_diag[label] = f"ERROR: {exc}"

                    return node_name, iid, r.status, r.output, issues, extra_diag
                except Exception as exc:
                    log.debug("Sweep failed for %s: %s", node_name[:40], exc)
                    return node_name, iid, "Error", str(exc), [], {}

            node_results = await asyncio.gather(
                *[_check_node(name, iid) for name, iid in sampled],
                return_exceptions=True,
            )

            for item in node_results:
                if isinstance(item, Exception):
                    continue
                node_name, iid, status, raw_output, node_issues, extra_diag = item
                detail: dict = {
                    "node_name": node_name, "instance_id": iid,
                    "status": status, "issues": node_issues,
                    "raw_output": raw_output,
                }
                if extra_diag:
                    detail["extra_diagnostics"] = extra_diag
                results["node_details"].append(detail)
                if node_issues:
                    results["issues"].extend(node_issues)
                    log.warning("  %s: %s", node_name[:40], "; ".join(node_issues))
                elif status == "Success":
                    results["healthy"] += 1
                else:
                    results["ssm_failed"] += 1

            log.info("SSM fallback sweep: %d/%d healthy, %d SSM failed, %d issues",
                     results["healthy"], results["nodes_sampled"],
                     results["ssm_failed"], len(results["issues"]))

        except Exception as exc:
            log.error("SSM fallback sweep failed: %s", exc)
        return results


class HealthSweepAgent:
    """Samples fleet health at peak load using AMP, K8s API, or SSM fallback."""

    def __init__(self, config: TestConfig, k8s_client,
                 node_diag: NodeDiagnosticsCollector,
                 evidence_store: EvidenceStore, run_id: str) -> None:
        self.config = config
        self.k8s_client = k8s_client
        self.node_diag = node_diag
        self.evidence_store = evidence_store
        self.run_id = run_id

    async def run(self, sample_size: int = 10) -> dict:
        """Execute health sweep. Returns Sweep_Result dict."""
        log.info("Node health sweep starting...")
        results: dict = {"nodes_sampled": 0, "healthy": 0, "issues": [],
                         "node_details": []}

        try:
            if self.config.amp_workspace_id or self.config.prometheus_url:
                # --- AMP / Prometheus + K8s conditions path ---
                amp_collector = AMPMetricCollector(
                    self.config.amp_workspace_id,
                    self.config.prometheus_url,
                    self.config.aws_profile,
                )
                k8s_checker = K8sConditionChecker(self.k8s_client)

                amp_metrics, k8s_conditions = await asyncio.gather(
                    amp_collector.collect_all(),
                    k8s_checker.check_all(),
                )

                # Capture raw PromQL responses for evidence
                raw_metrics: dict = {}
                for cat, metric_list in amp_metrics.items():
                    raw_metrics[cat] = [
                        {"node_name": m.node_name, "value": m.value}
                        for m in metric_list
                    ]

                results = self._merge_results(amp_metrics, k8s_conditions,
                                              raw_metrics)

                # If all AMP queries returned empty, flag it
                if not any(amp_metrics.values()):
                    results["issues"].append(
                        "All AMP metric queries failed")

                log.info(
                    "Node health sweep (AMP): %d nodes, %d healthy, %d issues",
                    results["nodes_sampled"], results["healthy"],
                    len(results["issues"]),
                )
            else:
                # --- SSM fallback path ---
                ssm_collector = SSMFallbackCollector(
                    self.k8s_client, self.node_diag)
                results = await ssm_collector.collect(sample_size)
                log.info(
                    "Node health sweep (SSM): %d nodes, %d healthy, %d issues",
                    results["nodes_sampled"], results["healthy"],
                    len(results["issues"]),
                )
        except Exception as exc:
            log.error("Node health sweep failed: %s", exc)

        # --- Evidence persistence (always, even on partial failure) ---
        try:
            sweep_path = (self.evidence_store._run_dir(self.run_id)
                          / "diagnostics" / "health_sweep.json")
            self.evidence_store._write_json(sweep_path, results)
            log.info("Health sweep data saved to %s", sweep_path)
        except Exception as exc:
            log.error("Failed to persist health sweep evidence: %s", exc)

        return results

    def _merge_results(self, amp_metrics: dict, k8s_conditions: list,
                       raw_metrics: dict) -> dict:
        """Merge AMP metrics and K8s conditions into Sweep_Result format."""
        all_nodes: set[str] = set()
        node_issues: dict[str, list[str]] = defaultdict(list)

        # Add AMP metric issues (threshold violations)
        for _category, metric_list in amp_metrics.items():
            for r in metric_list:
                all_nodes.add(r.node_name)
                issue = check_threshold(r)
                if issue:
                    node_issues[r.node_name].append(issue)

        # Add K8s condition issues
        for cond in k8s_conditions:
            all_nodes.add(cond.node_name)
            node_issues[cond.node_name].extend(cond.issues)

        # Build result
        issues: list[str] = []
        node_details: list[dict] = []
        healthy = 0
        for node in sorted(all_nodes):
            ni = node_issues.get(node, [])
            node_details.append({
                "node_name": node,
                "status": "Unhealthy" if ni else "Healthy",
                "issues": ni,
            })
            if ni:
                issues.extend(ni)
            else:
                healthy += 1

        return {
            "nodes_sampled": len(all_nodes),
            "healthy": healthy,
            "issues": issues,
            "node_details": node_details,
            "raw_metrics": raw_metrics,
        }


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
