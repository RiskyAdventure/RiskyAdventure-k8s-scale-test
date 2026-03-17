"""Node metrics analysis for identifying problem nodes."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from k8s_scale_test.models import (
    NodeCondition,
    NodeMetric,
    ProblemNode,
    TestConfig,
)

log = logging.getLogger(__name__)

# Conditions that indicate a problematic node
PROBLEM_CONDITIONS = {"MemoryPressure", "DiskPressure", "PIDPressure"}
DEFAULT_CPU_THRESHOLD = 90.0


class NodeMetricsAnalyzer:
    """Queries node metrics to identify problem nodes.

    Acts as a filter so SSM diagnostics are only dispatched to nodes
    that actually need inspection.
    """

    def __init__(
        self,
        config: TestConfig,
        k8s_client,
        prometheus_url: Optional[str] = None,
        cpu_threshold: float = DEFAULT_CPU_THRESHOLD,
    ) -> None:
        self.config = config
        self.k8s_client = k8s_client
        self.prometheus_url = prometheus_url
        self.cpu_threshold = cpu_threshold

    async def identify_problem_nodes(self) -> list[ProblemNode]:
        """Return nodes classified as problematic."""
        conditions = await self._query_node_conditions()

        if self.prometheus_url:
            metrics = await self._query_prometheus_metrics()
        else:
            metrics = await self._query_raw_metrics()

        # Build per-node lookup
        cond_by_node: dict[str, list[NodeCondition]] = {}
        for c in conditions:
            cond_by_node.setdefault(c.node_name, []).append(c)

        metric_by_node: dict[str, NodeMetric] = {}
        for m in metrics:
            metric_by_node[m.node_name] = m

        all_nodes = set(cond_by_node.keys()) | set(metric_by_node.keys())
        problems: list[ProblemNode] = []

        for node_name in all_nodes:
            node_conds = cond_by_node.get(node_name, [])
            node_metric = metric_by_node.get(node_name)
            reasons = self._classify_node_health(node_conds, node_metric)
            if reasons:
                # Resolve instance ID from provider ID
                instance_id = self._get_instance_id(node_name)
                problems.append(ProblemNode(
                    node_name=node_name,
                    instance_id=instance_id,
                    reasons=reasons,
                    metrics=node_metric or NodeMetric(
                        node_name=node_name, cpu_usage_pct=0.0,
                        memory_usage_pct=0.0, pod_count=0,
                        pods_ready=0, pods_not_ready=0,
                    ),
                    conditions=node_conds,
                ))

        return problems

    def _classify_node_health(
        self,
        conditions: list[NodeCondition],
        metrics: Optional[NodeMetric],
    ) -> list[str]:
        """Return list of reasons a node is problematic, or empty if healthy."""
        reasons: list[str] = []

        for c in conditions:
            if c.condition_type == "Ready" and c.status != "True":
                reasons.append("NotReady")
            elif c.condition_type in PROBLEM_CONDITIONS and c.status == "True":
                reasons.append(c.condition_type)

        if metrics and metrics.cpu_usage_pct > self.cpu_threshold:
            reasons.append("high_cpu")

        return reasons

    async def _query_node_conditions(self) -> list[NodeCondition]:
        """Get node conditions from K8s API."""
        try:
            loop = asyncio.get_event_loop()
            def _sync():
                v1 = self.k8s_client.CoreV1Api()
                nodes = v1.list_node(watch=False, _request_timeout=15)
                results: list[NodeCondition] = []
                for node in nodes.items:
                    for cond in (node.status.conditions or []):
                        results.append(NodeCondition(
                            node_name=node.metadata.name,
                            condition_type=cond.type,
                            status=cond.status,
                            reason=cond.reason or "",
                            message=cond.message or "",
                        ))
                return results
            return await loop.run_in_executor(None, _sync)
        except Exception as exc:
            log.error("Failed to query node conditions: %s", exc)
            return []

    async def _query_prometheus_metrics(self) -> list[NodeMetric]:
        """Query Prometheus for node metrics. Falls back to raw if unavailable."""
        log.info("Prometheus query not yet implemented, falling back to raw metrics")
        return await self._query_raw_metrics()

    async def _query_raw_metrics(self) -> list[NodeMetric]:
        """Get basic node metrics from node status. No pod listing needed."""
        try:
            loop = asyncio.get_event_loop()
            def _sync():
                v1 = self.k8s_client.CoreV1Api()
                nodes = v1.list_node(watch=False, _request_timeout=15)
                results: list[NodeMetric] = []
                for node in nodes.items:
                    name = node.metadata.name
                    cap = node.status.capacity or {}
                    alloc = node.status.allocatable or {}
                    cap_cpu = self._parse_cpu(cap.get("cpu", "0"))
                    alloc_cpu = self._parse_cpu(alloc.get("cpu", "0"))
                    cpu_pct = ((cap_cpu - alloc_cpu) / cap_cpu * 100) if cap_cpu > 0 else 0.0
                    results.append(NodeMetric(
                        node_name=name, cpu_usage_pct=cpu_pct,
                        memory_usage_pct=0.0, pod_count=0,
                        pods_ready=0, pods_not_ready=0,
                    ))
                return results
            return await loop.run_in_executor(None, _sync)
        except Exception as exc:
            log.error("Failed to query raw metrics: %s", exc)
            return []

    @staticmethod
    def _parse_cpu(value: str) -> float:
        """Parse K8s CPU string: '32' → 32, '15890m' → 15.89"""
        v = str(value).strip()
        if v.endswith("m"):
            return int(v[:-1]) / 1000
        return float(v)

    def _get_instance_id(self, node_name: str) -> str:
        """Resolve EC2 instance ID from node's providerID."""
        try:
            v1 = self.k8s_client.CoreV1Api()
            node = v1.read_node(node_name)
            provider_id = node.spec.provider_id or ""
            # Format: aws:///az/instance-id
            if "/" in provider_id:
                return provider_id.rsplit("/", 1)[-1]
            return provider_id
        except Exception as exc:
            log.error("Failed to resolve instance ID for %s: %s", node_name, exc)
            return ""

class CNIHealthAnalyzer:
    """Detects VPC CNI IPAMD failures by correlating stuck pods with ENI prefix state.

    Investigation path (mirrors manual troubleshooting):
    1. Find pods stuck in ContainerCreating with FailedCreatePodSandBox events
    2. Group stuck pods by node to identify affected nodes
    3. Query EC2 for ENI prefix counts on affected nodes
    4. Identify nodes with zero or abnormally low prefix counts
    5. Optionally pull IPAMD logs via SSM for confirmation
    """

    def __init__(self, k8s_client, aws_client, aws_profile: str | None = None) -> None:
        self.k8s_client = k8s_client
        self.aws_client = aws_client
        self.aws_profile = aws_profile

    async def diagnose_stuck_pods(self, namespace: str = "") -> dict:
        """Run full CNI health diagnosis with evidence-based root cause analysis.

        When nodes have zero prefixes, pulls the IPAMD log via SSM to determine
        the actual error — no guessing.
        """
        stuck_by_node = await self._find_stuck_pods_by_node(namespace)
        if not stuck_by_node:
            return {"affected_nodes": [], "total_stuck": 0, "root_cause": None,
                    "severity": "info", "recommendation": ""}

        total_stuck = sum(stuck_by_node.values())
        log.info("CNI diagnosis: %d pods stuck in ContainerCreating across %d nodes",
                 total_stuck, len(stuck_by_node))

        cni_failure_nodes = await self._check_sandbox_failures(stuck_by_node, namespace)
        if not cni_failure_nodes:
            return {"affected_nodes": [], "total_stuck": total_stuck,
                    "root_cause": "Pods stuck but no CNI sandbox failures detected",
                    "severity": "warning", "recommendation": "Check image pull status and node disk pressure"}

        node_eni_info = await self._check_eni_prefixes(cni_failure_nodes)
        zero_prefix_nodes = [n for n in node_eni_info if n["prefix_count"] == 0]
        low_prefix_nodes = [n for n in node_eni_info if 0 < n["prefix_count"] <= 2]

        if zero_prefix_nodes:
            # Pull IPAMD log from a sample node to get the ACTUAL error
            ipamd_evidence = await self._collect_ipamd_log(zero_prefix_nodes[0])
            root_cause, severity, recommendation = self._analyze_with_evidence(
                zero_prefix_nodes, node_eni_info, ipamd_evidence)
        elif low_prefix_nodes:
            root_cause = (
                f"IPAMD has insufficient prefixes on {len(low_prefix_nodes)} nodes "
                f"(1-2 prefixes, expected ~{self._expected_prefix_count()})."
            )
            severity = "warning"
            recommendation = "Monitor — IPAMD may recover. If not, restart aws-node on affected nodes."
        else:
            root_cause = (
                f"CNI sandbox failures on {len(cni_failure_nodes)} nodes but ENI prefix counts look normal."
            )
            severity = "warning"
            recommendation = "Check aws-node pod logs for errors."

        return {
            "affected_nodes": node_eni_info,
            "total_stuck": total_stuck,
            "root_cause": root_cause,
            "severity": severity,
            "recommendation": recommendation,
            "zero_prefix_nodes": len(zero_prefix_nodes),
            "low_prefix_nodes": len(low_prefix_nodes),
        }

    async def _find_stuck_pods_by_node(self, namespace: str) -> dict[str, int]:
        """Find pods in ContainerCreating/Pending state, grouped by node.

        Uses field_selector to only fetch Pending pods for performance at scale.
        """
        try:
            v1 = self.k8s_client.CoreV1Api()
            # Only fetch Pending pods — much faster than listing all pods
            if namespace:
                pods = v1.list_namespaced_pod(
                    namespace, field_selector="status.phase=Pending", watch=False,
                )
            else:
                pods = v1.list_pod_for_all_namespaces(
                    field_selector="status.phase=Pending", watch=False,
                )

            stuck_by_node: dict[str, int] = {}
            for pod in pods.items:
                node = pod.spec.node_name if pod.spec else None
                if not node:
                    continue  # Unscheduled pod — not a CNI issue
                stuck_by_node[node] = stuck_by_node.get(node, 0) + 1
            return stuck_by_node
        except Exception as exc:
            log.error("Failed to find stuck pods: %s", exc)
            return {}

    async def _check_sandbox_failures(self, stuck_by_node: dict[str, int], namespace: str) -> list[str]:
        """Check for FailedCreatePodSandBox events. If found, return affected nodes.

        Avoids per-pod lookups — just checks if the event pattern exists,
        then assumes all stuck nodes are affected (they're already stuck).
        """
        try:
            v1 = self.k8s_client.CoreV1Api()
            namespaces = [namespace] if namespace else ["default", "kube-system"]
            cni_failure_found = False
            for ns in namespaces:
                try:
                    events = v1.list_namespaced_event(
                        ns, field_selector="reason=FailedCreatePodSandBox", watch=False,
                    )
                    for ev in events.items:
                        if ev.message and "failed to assign an IP" in ev.message:
                            cni_failure_found = True
                            break
                except Exception:
                    continue
                if cni_failure_found:
                    break

            if cni_failure_found:
                # All nodes with stuck pods are likely affected
                return list(stuck_by_node.keys())
            else:
                # No CNI events — fall back to nodes with many stuck pods
                suspects = [n for n, count in stuck_by_node.items() if count >= 5]
                if suspects:
                    log.warning("No FailedCreatePodSandBox events found, but %d nodes have 5+ stuck pods",
                                len(suspects))
                return suspects
        except Exception as exc:
            log.error("Failed to check sandbox failures: %s", exc)
            return list(stuck_by_node.keys())[:20]

    async def _check_eni_prefixes(self, node_names: list[str]) -> list[dict]:
        """Query EC2 for ENI prefix counts and subnet state on specific nodes.

        For each node, collects: ENI count, prefix count, subnet ID, AZ.
        Also checks subnet available IPs to determine if exhaustion is the cause.
        """
        results: list[dict] = []
        subnet_ips_cache: dict[str, int] = {}
        sample = node_names[:10]
        try:
            ec2 = self.aws_client.client("ec2")
        except Exception as exc:
            log.error("EC2 client init failed: %s", exc)
            return results

        for node_name in sample:
            try:
                instance_id = self._get_instance_id(node_name)
                if not instance_id:
                    continue
                resp = ec2.describe_network_interfaces(
                    Filters=[{"Name": "attachment.instance-id", "Values": [instance_id]}]
                )
                total_prefixes = 0
                eni_count = 0
                subnet_id = ""
                az = ""
                for eni in resp.get("NetworkInterfaces", []):
                    eni_count += 1
                    prefixes = eni.get("Ipv4Prefixes") or []
                    total_prefixes += len(prefixes)
                    subnet_id = eni.get("SubnetId", subnet_id)
                    az = eni.get("AvailabilityZone", az)

                # Check subnet available IPs (cached per subnet)
                subnet_avail = -1
                if subnet_id and subnet_id not in subnet_ips_cache:
                    try:
                        sr = ec2.describe_subnets(SubnetIds=[subnet_id])
                        for s in sr.get("Subnets", []):
                            subnet_ips_cache[subnet_id] = s["AvailableIpAddressCount"]
                    except Exception:
                        pass
                subnet_avail = subnet_ips_cache.get(subnet_id, -1)

                results.append({
                    "node": node_name,
                    "instance_id": instance_id,
                    "eni_count": eni_count,
                    "prefix_count": total_prefixes,
                    "expected_ips": total_prefixes * 16,
                    "subnet_id": subnet_id,
                    "az": az,
                    "subnet_available_ips": subnet_avail,
                })
            except Exception as exc:
                log.error("Failed to check ENI prefixes for %s: %s", node_name, exc)
                results.append({
                    "node": node_name, "instance_id": "", "eni_count": -1,
                    "prefix_count": -1, "expected_ips": 0,
                    "subnet_id": "", "az": "", "subnet_available_ips": -1,
                })
        return results

    def _get_instance_id(self, node_name: str) -> str:
        try:
            v1 = self.k8s_client.CoreV1Api()
            node = v1.read_node(node_name)
            provider_id = node.spec.provider_id or ""
            if "/" in provider_id:
                return provider_id.rsplit("/", 1)[-1]
            return provider_id
        except Exception as exc:
            log.error("Failed to resolve instance ID for %s: %s", node_name, exc)
            return ""

    def _expected_prefix_count(self) -> int:
        return 10

    async def _collect_ipamd_log(self, node_info: dict) -> str:
        """Pull IPAMD log from a node via SSM. Returns log content or error."""
        instance_id = node_info.get("instance_id", "")
        if not instance_id:
            return "ERROR: no instance_id"
        try:
            import time
            ssm = self.aws_client.client("ssm")
            resp = ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [
                    "tail -200 /var/log/aws-routed-eni/ipamd.log 2>/dev/null || echo NO_IPAMD_LOG"
                ]},
                TimeoutSeconds=30,
            )
            cmd_id = resp["Command"]["CommandId"]
            for _ in range(30):
                time.sleep(1)
                inv = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
                if inv["Status"] in ("Success", "Failed", "TimedOut", "Cancelled"):
                    return inv.get("StandardOutputContent", "") or inv.get("StandardErrorContent", "")
            return "ERROR: SSM timed out"
        except Exception as exc:
            return f"ERROR: {exc}"

    def _analyze_with_evidence(
        self, zero_nodes: list[dict], all_nodes: list[dict], ipamd_log: str,
    ) -> tuple[str, str, str]:
        """Determine root cause using actual IPAMD log evidence + ENI/subnet data."""
        # Group by AZ
        by_az: dict[str, list[dict]] = {}
        exhausted_azs: list[str] = []
        for n in zero_nodes:
            az = n.get("az", "unknown")
            by_az.setdefault(az, []).append(n)
            avail = n.get("subnet_available_ips", -1)
            if 0 <= avail < 100 and az not in exhausted_azs:
                exhausted_azs.append(az)

        # Parse IPAMD log for actual errors
        ipamd_lower = ipamd_log.lower()
        has_throttle = "throttl" in ipamd_lower or "rate exceeded" in ipamd_lower or "requestlimitexceeded" in ipamd_lower
        has_no_ips = "no available ip/prefix" in ipamd_lower or "datastore has no available" in ipamd_lower
        has_ec2_error = "ec2api" in ipamd_lower and "error" in ipamd_lower
        has_auth_error = "unauthorized" in ipamd_lower or "accessdenied" in ipamd_lower or "forbidden" in ipamd_lower
        has_no_log = "no_ipamd_log" in ipamd_lower or "error:" in ipamd_log[:20].lower()
        ipamd_silent = has_no_ips and not has_throttle and not has_ec2_error and not has_auth_error

        # Case 1: Subnet IP exhaustion
        if exhausted_azs:
            az_details = []
            for az in exhausted_azs:
                nodes_in_az = by_az.get(az, [])
                avail = nodes_in_az[0].get("subnet_available_ips", 0) if nodes_in_az else 0
                subnet = nodes_in_az[0].get("subnet_id", "?") if nodes_in_az else "?"
                az_details.append(f"{az}: subnet {subnet} has {avail} IPs remaining")
            root_cause = (
                f"Subnet IP exhaustion in {len(exhausted_azs)} AZ(s). "
                f"Nodes cannot allocate /28 prefixes — subnets nearly full. "
                + "; ".join(az_details)
            )
            recommendation = (
                f"Add secondary CIDR blocks to VPC and create new subnets in: {', '.join(exhausted_azs)}. "
                f"Each node needs ~160 IPs (10 prefixes × 16 IPs). Consider a /16 CIDR."
            )
            return root_cause, "critical", recommendation

        # Case 2: EC2 API throttling confirmed by IPAMD log
        if has_throttle:
            root_cause = (
                f"{len(zero_nodes)} nodes have 0 prefixes. IPAMD log confirms EC2 API throttling: "
                f"AssignIpv4Addresses calls were rate-limited. "
                f"This happens when many nodes start simultaneously and all call EC2 at once."
            )
            recommendation = (
                f"Restart aws-node on affected nodes (IPAMD will retry). "
                f"To prevent: stagger node provisioning or request EC2 API rate limit increase."
            )
            return root_cause, "critical", recommendation

        # Case 3: EC2 API auth/permission error
        if has_auth_error:
            root_cause = (
                f"{len(zero_nodes)} nodes have 0 prefixes. IPAMD log shows authorization failure — "
                f"the node's IAM role cannot call EC2 AssignIpv4Addresses."
            )
            recommendation = (
                "Check the Karpenter node IAM role has ec2:AssignPrivateIpAddresses "
                "and ec2:AssignIpv6Addresses permissions."
            )
            return root_cause, "critical", recommendation

        # Case 4: IPAMD datastore empty, no specific EC2 error — silent failure
        if ipamd_silent:
            root_cause = (
                f"{len(zero_nodes)} nodes have 0 prefixes across {len(by_az)} AZ(s). "
                f"IPAMD log shows 'no available IP/Prefix addresses' but no EC2 API errors. "
                f"Subnets have available IPs. IPAMD started but never successfully allocated prefixes — "
                f"the reconciliation loop failed silently during node initialization."
            )
            recommendation = (
                f"Restart aws-node pods on the {len(zero_nodes)} affected nodes. "
                f"This is an IPAMD initialization bug — the daemon started but its "
                f"background prefix allocator never ran successfully."
            )
            return root_cause, "critical", recommendation

        # Case 5: Couldn't get IPAMD log
        if has_no_log:
            root_cause = (
                f"{len(zero_nodes)} nodes have 0 prefixes. Could not retrieve IPAMD log via SSM. "
                f"Subnets have IPs. Cannot determine specific cause without IPAMD log."
            )
            recommendation = (
                f"Manually check /var/log/aws-routed-eni/ipamd.log on affected nodes. "
                f"Restart aws-node pods as immediate mitigation."
            )
            return root_cause, "critical", recommendation

        # Case 6: IPAMD log has other errors
        # Extract a sample error line
        error_lines = [l for l in ipamd_log.split("\n") if "error" in l.lower()][:3]
        sample = error_lines[0][:200] if error_lines else "no error lines found"
        root_cause = (
            f"{len(zero_nodes)} nodes have 0 prefixes. IPAMD log errors: {sample}"
        )
        recommendation = (
            f"Review full IPAMD log. Restart aws-node pods on affected nodes as mitigation."
        )
        return root_cause, "critical", recommendation


