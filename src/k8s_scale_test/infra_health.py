"""Infrastructure health agent — checks control plane components.

Currently checks Karpenter controller pod health (restarts, CPU/memory
usage via metrics API). Uses K8s API only, no SSM. Triggered when
InsufficientCapacityError findings are detected during scaling.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class InfraHealthAgent:
    """Checks infrastructure component health via K8s API."""

    def __init__(self, k8s_client) -> None:
        self.k8s_client = k8s_client

    async def check_karpenter(self) -> dict:
        """Check Karpenter controller pod resource usage.

        When InsufficientCapacityError events fire, the question is: is EC2
        actually out of capacity, or is Karpenter itself resource-starved?

        On test clusters, CPU/memory limits on Karpenter add latency via
        throttling. We flag limits as an issue and recommend removing them.
        """
        result = {"checked": False, "pod_name": "", "cpu_usage": "",
                  "memory_usage": "", "restarts": 0, "issues": [],
                  "cpu_limit": "", "memory_limit": ""}
        try:
            loop = asyncio.get_event_loop()
            v1 = self.k8s_client.CoreV1Api()

            # Find the karpenter controller pod
            pods = await loop.run_in_executor(None,
                lambda: v1.list_namespaced_pod(
                    "kube-system",
                    label_selector="app.kubernetes.io/name=karpenter",
                    watch=False, _request_timeout=10))
            if not pods.items:
                pods = await loop.run_in_executor(None,
                    lambda: v1.list_namespaced_pod(
                        "karpenter",
                        label_selector="app.kubernetes.io/name=karpenter",
                        watch=False, _request_timeout=10))
            if not pods.items:
                log.debug("Karpenter pod not found — skipping health check")
                return result

            pod = pods.items[0]
            result["checked"] = True
            result["pod_name"] = pod.metadata.name

            # Extract resource limits from the pod spec
            cpu_limit_str = ""
            mem_limit_str = ""
            for container in (pod.spec.containers or []):
                if container.name == "controller":
                    limits = (container.resources.limits or {}) if container.resources else {}
                    cpu_limit_str = limits.get("cpu", "")
                    mem_limit_str = limits.get("memory", "")
                    result["cpu_limit"] = cpu_limit_str
                    result["memory_limit"] = mem_limit_str
                    break

            # Flag resource limits as a performance issue on test clusters.
            # Limits cause CPU throttling and OOM kills under load — for scale
            # testing, Karpenter should run without limits so it can burst freely.
            if cpu_limit_str:
                result["issues"].append(
                    f"Karpenter has CPU limit ({cpu_limit_str}) — causes throttling under load, "
                    f"recommend removing for scale tests")
            if mem_limit_str:
                result["issues"].append(
                    f"Karpenter has memory limit ({mem_limit_str}) — risk of OOM under load, "
                    f"recommend removing for scale tests")

            for cs in (pod.status.container_statuses or []):
                if cs.name == "controller":
                    result["restarts"] = cs.restart_count or 0
                    if result["restarts"] > 0:
                        result["issues"].append(
                            f"Karpenter controller restarted {result['restarts']} times")
                    break

            # Check resource usage via metrics API if available
            try:
                custom = self.k8s_client.CustomObjectsApi()
                ns = pod.metadata.namespace
                metrics = await loop.run_in_executor(None,
                    lambda: custom.get_namespaced_custom_object(
                        "metrics.k8s.io", "v1beta1", ns,
                        "pods", pod.metadata.name))
                for c in metrics.get("containers", []):
                    if c.get("name") == "controller":
                        cpu = c.get("usage", {}).get("cpu", "")
                        mem = c.get("usage", {}).get("memory", "")
                        result["cpu_usage"] = cpu
                        result["memory_usage"] = mem
                        break
            except Exception:
                log.debug("Metrics API unavailable for Karpenter — skipping resource check")

            if result["issues"]:
                log.warning("Karpenter health: %s", "; ".join(result["issues"]))
            else:
                log.info("Karpenter health: OK (pod=%s, restarts=%d, no limits set)",
                         result["pod_name"], result["restarts"])
        except Exception as exc:
            log.debug("Karpenter health check failed: %s", exc)
        return result

    async def check_if_needed(self, findings: list) -> dict:
        """Run Karpenter check only if InsufficientCapacityError was seen."""
        ice = any(f.root_cause and "InsufficientCapacityError" in f.root_cause
                  for f in findings)
        if ice:
            return await self.check_karpenter()
        return {}


def _parse_cpu_millicores(cpu: str) -> int:
    """Parse K8s CPU string to millicores: '500m' -> 500, '1' -> 1000."""
    if not cpu:
        return 0
    try:
        if cpu.endswith("m"):
            return int(cpu[:-1])
        if cpu.endswith("n"):
            return int(cpu[:-1]) // 1_000_000
        return int(float(cpu) * 1000)
    except (ValueError, TypeError):
        return 0


def _parse_memory_mi(mem: str) -> int:
    """Parse K8s memory string to MiB: '512Mi' -> 512, '1Gi' -> 1024."""
    if not mem:
        return 0
    try:
        if mem.endswith("Ki"):
            return int(mem[:-2]) // 1024
        if mem.endswith("Mi"):
            return int(mem[:-2])
        if mem.endswith("Gi"):
            return int(float(mem[:-2]) * 1024)
        return 0
    except (ValueError, TypeError):
        return 0
