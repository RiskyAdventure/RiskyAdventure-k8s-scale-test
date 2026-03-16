"""Anomaly detection — general-purpose evidence collection and correlation.

Collects evidence from every available layer (K8s events, pod state by phase,
node state, EC2 ENI state, SSM node logs), then correlates. Dynamic SSM command
selection based on what events reveal. No hardcoded failure-mode detectors.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable, Optional

from k8s_scale_test.diagnostics import NodeDiagnosticsCollector
from k8s_scale_test.evidence import EvidenceStore
from k8s_scale_test.metrics import NodeMetricsAnalyzer
from k8s_scale_test.models import (
    Alert, Finding, K8sEvent, NodeDiagnostic, NodeMetric,
    ProblemNode, Severity, TestConfig,
)

log = logging.getLogger(__name__)


class AnomalyDetector:
    def __init__(
        self, config: TestConfig, k8s_client,
        node_metrics: NodeMetricsAnalyzer,
        node_diag: NodeDiagnosticsCollector,
        evidence_store: EvidenceStore, run_id: str,
        operator_cb: Optional[Callable[[str, dict], Awaitable[str]]] = None,
        aws_client=None,
    ) -> None:
        self.config = config
        self.k8s_client = k8s_client
        self.node_metrics = node_metrics
        self.node_diag = node_diag
        self.evidence_store = evidence_store
        self.run_id = run_id
        self._operator_cb = operator_cb
        self.aws_client = aws_client
        self._node_cache: dict[str, str] = {}  # node_name -> instance_id

    async def handle_alert(self, alert: Alert) -> Finding:
        """Investigate by collecting evidence from all layers, then correlate."""
        log.info("Investigating: %s", alert.message)
        ns_list = alert.context.get("namespaces", ["default"])
        window = timedelta(minutes=self.config.event_time_window_minutes)

        # Layer 1: K8s events — the starting point for any investigation
        events = await self._collect_k8s_events(ns_list, window)
        warning_reasons = self._count_warning_reasons(events)
        if warning_reasons:
            log.info("  Events: %s", ", ".join(f"{r}={c}" for r, c in
                     sorted(warning_reasons.items(), key=lambda x: -x[1])[:5]))

        # Layer 2: Pod phase breakdown — distinguish Pending vs ContainerCreating
        phase_breakdown = await self._get_pod_phase_breakdown(ns_list)
        if phase_breakdown:
            log.info("  Pod phases: %s", ", ".join(f"{k}={v}" for k, v in phase_breakdown.items() if v > 0))

        # Layer 3: Nodes with stuck pods (scheduled but not running)
        stuck_nodes = await self._find_stuck_pod_nodes(ns_list)

        # Layer 4: Standard node health (conditions)
        condition_problems = await self.node_metrics.identify_problem_nodes()

        # Merge investigation targets
        targets = self._merge_targets(stuck_nodes, condition_problems)
        if targets:
            log.info("  Investigating %d nodes (%d stuck-pod, %d bad-condition)",
                     len(targets), len(stuck_nodes), len(condition_problems))

        # Layer 5: EC2 ENI/prefix state (if AWS client available)
        eni_evidence = {}
        if self.aws_client and targets:
            eni_evidence = await self._collect_eni_state(targets[:10])

        # Layer 6: SSM — dynamic commands based on what events tell us
        extra_cmds = self._pick_ssm_commands(warning_reasons, eni_evidence)
        diags: list[NodeDiagnostic] = []
        for node_name, iid in targets[:3]:
            if iid:
                diags.append(await self.node_diag.collect(node_name, iid,
                             extra_commands=extra_cmds))

        # Correlate everything
        metrics = [pn.metrics for pn in condition_problems]
        finding = self._correlate(alert, events, metrics, diags,
                                  stuck_nodes, eni_evidence, phase_breakdown)
        self.evidence_store.save_finding(self.run_id, finding)

        if finding.root_cause is None and self._operator_cb:
            await self._operator_cb(
                f"Unresolved: {finding.symptom}",
                {"finding_id": finding.finding_id, "severity": finding.severity.value,
                 "evidence": finding.evidence_references[:5]},
            )
        return finding

    # ------------------------------------------------------------------
    # Evidence collection
    # ------------------------------------------------------------------

    async def _collect_k8s_events(self, namespaces, time_window):
        """Collect Warning events. Events are cleared before each test,
        so everything here is from the current run. Paginate to handle volume."""
        result = []
        loop = asyncio.get_event_loop()
        try:
            v1 = self.k8s_client.CoreV1Api()
            for ns in namespaces:
                _continue = None
                while True:
                    try:
                        kwargs = {"watch": False, "limit": 200,
                                  "field_selector": "type=Warning"}
                        if _continue:
                            kwargs["_continue"] = _continue
                        resp = await loop.run_in_executor(None,
                            lambda kw=kwargs: v1.list_namespaced_event(ns, **kw))
                        for ev in resp.items:
                            ts = ev.last_timestamp or ev.event_time
                            if ts:
                                ts = ts.replace(tzinfo=timezone.utc)
                            else:
                                ts = datetime.now(timezone.utc)
                            result.append(K8sEvent(
                                timestamp=ts, namespace=ns,
                                involved_object_kind=ev.involved_object.kind or "",
                                involved_object_name=ev.involved_object.name or "",
                                reason=ev.reason or "", message=ev.message or "",
                                event_type="Warning",
                                scaling_step=0, count=ev.count or 1,
                            ))
                        _continue = resp.metadata._continue if resp.metadata else None
                        if not _continue:
                            break
                    except Exception as exc:
                        log.error("Event page failed for %s: %s", ns, exc)
                        break
        except Exception as exc:
            log.error("Event collection failed: %s", exc)
        return result

    async def _get_pod_phase_breakdown(self, namespaces):
        """Get pod phase counts from deployment status — no pod listing needed."""
        phases = {"Pending": 0, "ContainerCreating": 0, "Running": 0,
                  "Succeeded": 0, "Failed": 0, "Unknown": 0}
        try:
            loop = asyncio.get_event_loop()
            def _count():
                apps_v1 = self.k8s_client.AppsV1Api()
                for ns in namespaces:
                    deps = apps_v1.list_namespaced_deployment(ns, watch=False, _request_timeout=10)
                    for d in deps.items:
                        r = d.status.replicas or 0
                        rd = d.status.ready_replicas or 0
                        phases["Running"] += rd
                        phases["Pending"] += r - rd
                return phases
            return await loop.run_in_executor(None, _count)
        except Exception as exc:
            log.error("Pod phase breakdown failed: %s", exc)
            return phases

    async def _find_stuck_pod_nodes(self, namespaces):
        """Find nodes with Pending pods. Uses field_selector + limit to keep response small."""
        try:
            loop = asyncio.get_event_loop()
            def _scan():
                v1 = self.k8s_client.CoreV1Api()
                if not self._node_cache:
                    nodes = v1.list_node(watch=False)
                    for n in nodes.items:
                        pid = n.spec.provider_id or ""
                        self._node_cache[n.metadata.name] = pid.rsplit("/", 1)[-1] if "/" in pid else ""
                by_node: dict[str, int] = {}
                for ns in namespaces:
                    # Limit to 200 pods — enough to identify problem nodes without huge response
                    pods = v1.list_namespaced_pod(ns, field_selector="status.phase=Pending",
                                                  limit=200, watch=False, _request_timeout=15)
                    for pod in pods.items:
                        node = pod.spec.node_name if pod.spec else None
                        if node:
                            by_node[node] = by_node.get(node, 0) + 1
                result = []
                for node, count in sorted(by_node.items(), key=lambda x: -x[1])[:20]:
                    iid = self._node_cache.get(node, "")
                    result.append((node, iid))
                return result
            return await loop.run_in_executor(None, _scan)
            return result
        except Exception as exc:
            log.error("Stuck pod scan failed: %s", exc)
            return []

    async def _collect_eni_state(self, nodes):
        """Query EC2 ENI/prefix counts + subnet IPs. Runs in executor to not block event loop."""
        import asyncio
        loop = asyncio.get_event_loop()
        result = {}
        try:
            ec2 = self.aws_client.client("ec2")
            subnet_cache = {}
            for node_name, iid in nodes:
                if not iid:
                    continue
                try:
                    resp = await loop.run_in_executor(None, lambda i=iid: ec2.describe_network_interfaces(
                        Filters=[{"Name": "attachment.instance-id", "Values": [i]}]))
                    eni_count = prefix_count = 0
                    subnet_id = az = ""
                    for eni in resp.get("NetworkInterfaces", []):
                        eni_count += 1
                        prefix_count += len(eni.get("Ipv4Prefixes") or [])
                        subnet_id = eni.get("SubnetId", subnet_id)
                        az = eni.get("AvailabilityZone", az)
                    if subnet_id and subnet_id not in subnet_cache:
                        try:
                            sr = await loop.run_in_executor(None, lambda s=subnet_id: ec2.describe_subnets(SubnetIds=[s]))
                            for s in sr.get("Subnets", []):
                                subnet_cache[subnet_id] = s["AvailableIpAddressCount"]
                        except Exception:
                            pass
                    result[node_name] = {
                        "instance_id": iid, "eni_count": eni_count,
                        "prefix_count": prefix_count, "subnet_id": subnet_id,
                        "az": az, "subnet_available_ips": subnet_cache.get(subnet_id, -1),
                    }
                except Exception as exc:
                    log.error("ENI check failed for %s: %s", node_name, exc)
        except Exception as exc:
            log.error("EC2 client failed: %s", exc)
        return result

    # ------------------------------------------------------------------
    # Dynamic SSM command selection — evidence-driven
    # ------------------------------------------------------------------

    def _pick_ssm_commands(self, warning_reasons, eni_evidence):
        """Choose extra SSM commands based on what events and EC2 data show.

        Each command here adds an SSM send_command call per investigated node
        (up to 3 nodes). Only add commands when evidence justifies them.
        """
        extra = {}
        zero_pfx = any(v.get("prefix_count", -1) == 0 for v in eni_evidence.values())

        # --- CNI / IPAMD checks — triggered by sandbox failures or zero prefixes ---
        if warning_reasons.get("FailedCreatePodSandBox", 0) > 0 or zero_pfx:
            extra["ipamd_log"] = "tail -200 /var/log/aws-routed-eni/ipamd.log 2>/dev/null || echo NO_IPAMD_LOG"
            extra["cni_config"] = "cat /etc/cni/net.d/* 2>/dev/null | head -50"

        # --- Disk / image checks — triggered by pull failures ---
        if warning_reasons.get("Failed", 0) > 0 or warning_reasons.get("ErrImagePull", 0) > 0:
            extra["disk_images"] = "df -h; echo '---'; crictl images 2>/dev/null | tail -20"

        # --- Memory checks — triggered by evictions or OOM ---
        if warning_reasons.get("Evicted", 0) > 0 or warning_reasons.get("OOMKilling", 0) > 0:
            extra["memory"] = "cat /proc/meminfo | head -10; echo '---'; dmesg | grep -i oom | tail -10"
            # PSI confirms memory pressure before OOM — only when we see memory events
            extra["psi"] = (
                "cat /proc/pressure/cpu 2>/dev/null; echo '===PSI_SEP==='; "
                "cat /proc/pressure/memory 2>/dev/null; echo '===PSI_SEP==='; "
                "cat /proc/pressure/io 2>/dev/null"
            )

        # --- Scheduling checks — triggered by FailedScheduling ---
        if warning_reasons.get("FailedScheduling", 0) > 0:
            extra["kubelet_cfg"] = "cat /etc/kubernetes/kubelet/kubelet-config.json 2>/dev/null | head -30"

        # --- Node readiness checks — triggered by NotReady or InvalidDiskCapacity ---
        if warning_reasons.get("NodeNotReady", 0) > 0:
            extra["kubelet_status"] = "systemctl status kubelet --no-pager -l 2>&1 | tail -20"
            # Kubelet healthz catches TLS bootstrap delays seen in scale test logs
            extra["kubelet_health"] = (
                "curl -sk https://localhost:10250/healthz 2>&1; echo '===HEALTH_SEP==='; "
                "systemctl is-active kubelet 2>&1"
            )

        if warning_reasons.get("InvalidDiskCapacity", 0) > 0:
            # NVMe readiness: catches the i4i root volume setup race
            extra["disk_readiness"] = "lsblk 2>/dev/null | grep nvme; echo '---'; df -h / 2>/dev/null"

        # --- Per-core CPU — only when nodes show high CPU or stuck pods with no other cause ---
        # mpstat takes 1s to sample, so only run when we suspect CPU saturation
        total_warnings = sum(warning_reasons.values())
        if (warning_reasons.get("Evicted", 0) > 0
                or (total_warnings > 0 and not extra)):
            # No specific cause identified but something is wrong — check CPU cores
            extra["mpstat"] = "mpstat -P ALL 1 1 2>/dev/null | tail -n +4 || echo NO_MPSTAT"

        return extra

    # ------------------------------------------------------------------
    # Correlation — evidence-driven
    # ------------------------------------------------------------------

    def _correlate(self, alert, events, metrics, diagnostics,
                   stuck_nodes, eni_evidence, phase_breakdown):
        warnings = [e for e in events if e.event_type == "Warning"]
        warning_reasons = self._count_warning_reasons(events)
        evidence_refs = []

        # Phase breakdown evidence
        if phase_breakdown:
            pending = phase_breakdown.get("Pending", 0)
            creating = phase_breakdown.get("ContainerCreating", 0)
            evidence_refs.append(f"pods:pending={pending},creating={creating}")

        # Event evidence
        if warning_reasons:
            top = sorted(warning_reasons.items(), key=lambda x: -x[1])[:5]
            evidence_refs.append("warnings:" + ",".join(f"{r}={c}" for r, c in top))

        # ENI evidence
        if eni_evidence:
            zero = sum(1 for v in eni_evidence.values() if v.get("prefix_count", -1) == 0)
            evidence_refs.append(f"eni:checked={len(eni_evidence)},zero_prefix={zero}")

        # SSM evidence
        if diagnostics:
            evidence_refs.append(f"ssm_diags:{len(diagnostics)}")

        # Proactive node health evidence (PSI, kubelet, disk, mpstat)
        proactive_clues = []
        for d in diagnostics:
            proactive_clues.extend(self._parse_psi_evidence(d))
            proactive_clues.extend(self._parse_kubelet_health_evidence(d))
            proactive_clues.extend(self._parse_disk_readiness_evidence(d))
            proactive_clues.extend(self._parse_mpstat_evidence(d))
        if proactive_clues:
            evidence_refs.append(f"node_health:{len(proactive_clues)}_issues")

        # Determine severity from evidence weight
        severity = self._assess_severity(warnings, stuck_nodes, eni_evidence)

        # Extract root cause from all evidence
        root_cause = self._extract_root_cause(
            warning_reasons, eni_evidence, diagnostics, phase_breakdown)

        affected = list({e.involved_object_name for e in events})[:20]
        affected.extend(n for n, _ in stuck_nodes[:10])

        # Cap events stored per finding — keep a representative sample
        # Group by reason, keep up to 5 per reason, max 50 total
        sampled_events = []
        reason_counts: dict[str, int] = {}
        for e in events:
            r = e.reason
            reason_counts[r] = reason_counts.get(r, 0) + 1
            if reason_counts[r] <= 5:
                sampled_events.append(e)
        sampled_events = sampled_events[:50]

        return Finding(
            finding_id=f"finding-{uuid.uuid4().hex[:8]}",
            timestamp=datetime.now(timezone.utc),
            severity=severity, symptom=alert.message,
            affected_resources=affected, k8s_events=sampled_events,
            node_metrics=metrics, node_diagnostics=diagnostics,
            evidence_references=evidence_refs,
            root_cause=root_cause, resolved=root_cause is not None,
        )

    def _assess_severity(self, warnings, stuck_nodes, eni_evidence):
        zero_pfx = sum(1 for v in eni_evidence.values() if v.get("prefix_count", -1) == 0)
        if zero_pfx > 0:
            return Severity.CRITICAL
        if len(stuck_nodes) > 10 or len(warnings) > 50:
            return Severity.CRITICAL
        if stuck_nodes or len(warnings) > 10:
            return Severity.WARNING
        return Severity.INFO

    def _extract_root_cause(self, warning_reasons, eni_evidence,
                            diagnostics, phase_breakdown):
        """Scan all evidence for root cause. Returns None if inconclusive."""
        clues = []

        # --- Karpenter / capacity issues (most common at scale) ---
        ice = warning_reasons.get("InsufficientCapacityError", 0)
        if ice > 0:
            clues.append(f"InsufficientCapacityError x{ice} — Karpenter cannot provision nodes "
                         f"(spot exhaustion or NodePool limit reached)")

        idc = warning_reasons.get("InvalidDiskCapacity", 0)
        if idc > 0:
            # Usually transient on i4i — NVMe root volume setup race during boot
            # Only flag if it persists alongside other capacity issues
            if ice == 0 and idc > 50:
                clues.append(f"InvalidDiskCapacity x{idc} — persistent disk setup failures")

        tgpe = warning_reasons.get("TerminationGracePeriodExpiring", 0)
        if tgpe > 0 and ice > 0:
            # These pair with InsufficientCapacityError — failed nodeclaims expiring
            clues.append(f"TerminationGracePeriodExpiring x{tgpe} — failed nodeclaim cleanup")

        dnf = warning_reasons.get("DeletingNodeFailed", 0)
        if dnf > 0:
            clues.append(f"DeletingNodeFailed x{dnf} — node deletion issues during scaling")

        fd = warning_reasons.get("FailedDraining", 0)
        if fd > 0:
            clues.append(f"FailedDraining x{fd} — nodes being replaced mid-scale")

        # --- Pod sandbox / CNI issues ---
        fcps = warning_reasons.get("FailedCreatePodSandBox", 0)
        if fcps > 10:
            clues.append(f"FailedCreatePodSandBox x{fcps} — VPC CNI failures "
                         f"(likely MAC collision at high pod density)")

        fcpc = warning_reasons.get("FailedCreatePodContainer", 0)
        if fcpc > 0:
            clues.append(f"FailedCreatePodContainer x{fcpc} — container runtime errors")

        # --- ENI/prefix evidence ---
        zero_nodes = [n for n, v in eni_evidence.items() if v.get("prefix_count", -1) == 0]
        if zero_nodes:
            exhausted = [n for n in zero_nodes
                         if 0 <= eni_evidence[n].get("subnet_available_ips", -1) < 100]
            if exhausted:
                azs = {eni_evidence[n].get("az", "?") for n in exhausted}
                clues.append(f"Subnet IP exhaustion in {', '.join(azs)}")
            else:
                clues.append(f"{len(zero_nodes)} nodes have 0 prefixes — IPAMD failure")

        # --- SSM log evidence (existing + new proactive checks) ---
        for d in diagnostics:
            # Existing log-based checks
            for field in ["kubelet_logs", "containerd_logs", "journal_kubelet",
                          "journal_containerd", "resource_utilization"]:
                r = getattr(d, field, None)
                if not r or r.status != "Success" or not r.output:
                    continue
                low = r.output.lower()
                if "no available ip/prefix" in low:
                    clues.append(f"IPAMD on {d.node_name}: empty datastore")
                if "throttl" in low or "rate exceeded" in low or "requestlimitexceeded" in low:
                    clues.append(f"EC2 API throttling on {d.node_name}")
                if "oom" in low and "kill" in low:
                    clues.append(f"OOM kill on {d.node_name}")
                if "disk pressure" in low:
                    clues.append(f"Disk pressure on {d.node_name}")
                if "accessdeniedexception" in low:
                    clues.append(f"IAM auth failure on {d.node_name}")
                if "failed to generate unique mac" in low:
                    clues.append(f"VPC CNI MAC collision on {d.node_name}")

            # --- Proactive checks from SSM extra commands ---
            clues.extend(self._parse_psi_evidence(d))
            clues.extend(self._parse_kubelet_health_evidence(d))
            clues.extend(self._parse_disk_readiness_evidence(d))
            clues.extend(self._parse_mpstat_evidence(d))

        # --- Phase-based clues ---
        if phase_breakdown:
            pending = phase_breakdown.get("Pending", 0)
            creating = phase_breakdown.get("ContainerCreating", 0)
            if pending > 100:
                clues.append(f"{pending} pods unschedulable")
            if creating > 100 and pending == 0:
                clues.append(f"{creating} pods stuck in ContainerCreating")

        if not clues:
            return None
        return "; ".join(clues)

    # ------------------------------------------------------------------
    # Proactive node health parsers (ported from MCP audit findings)
    # ------------------------------------------------------------------

    def _parse_psi_evidence(self, diag):
        """Parse PSI (Pressure Stall Information) from SSM output.

        Detects CPU/memory/IO contention BEFORE it manifests as pod failures.
        PSI avg10 > 25% for CPU or > 10% for IO indicates active stalling.
        """
        clues = []
        r = getattr(diag, "resource_utilization", None)
        if not r or r.status != "Success" or not r.output:
            return clues
        # PSI output is appended to resource_utilization via extra commands
        if "===PSI_SEP===" not in r.output:
            return clues
        try:
            # Split the PSI sections: cpu, memory, io
            psi_start = r.output.index("=== psi ===\n") if "=== psi ===" in r.output else -1
            psi_text = r.output
            if "===PSI_SEP===" in psi_text:
                sections = psi_text.split("===PSI_SEP===")
                for section in sections:
                    avg10 = self._extract_psi_avg10(section)
                    if avg10 is None:
                        continue
                    section_lower = section.lower()
                    if "cpu" in section_lower or sections.index(section) == 0:
                        if avg10 > 25.0:
                            clues.append(f"CPU pressure avg10={avg10:.1f}% on {diag.node_name}")
                    elif "memory" in section_lower or sections.index(section) == 1:
                        if avg10 > 25.0:
                            clues.append(f"Memory pressure avg10={avg10:.1f}% on {diag.node_name}")
                    elif "io" in section_lower or sections.index(section) == 2:
                        if avg10 > 10.0:
                            clues.append(f"IO pressure avg10={avg10:.1f}% on {diag.node_name}")
        except Exception:
            pass
        return clues

    @staticmethod
    def _extract_psi_avg10(text):
        """Extract avg10 value from a PSI 'some' line.

        Format: 'some avg10=0.50 avg60=0.25 avg300=0.10 total=12345'
        """
        for line in text.strip().split("\n"):
            if line.startswith("some "):
                for part in line.split():
                    if part.startswith("avg10="):
                        try:
                            return float(part.split("=", 1)[1])
                        except ValueError:
                            return None
        return None

    def _parse_kubelet_health_evidence(self, diag):
        """Parse kubelet healthz + systemctl output.

        Catches TLS bootstrap delays (massive volume in real scale test logs)
        and kubelet not-active states before K8s conditions propagate.
        """
        clues = []
        r = getattr(diag, "resource_utilization", None)
        if not r or r.status != "Success" or not r.output:
            return clues
        if "===HEALTH_SEP===" not in r.output:
            return clues
        try:
            parts = r.output.split("===HEALTH_SEP===")
            healthz = parts[0].strip() if len(parts) > 0 else ""
            systemctl = parts[1].strip() if len(parts) > 1 else ""

            if healthz and healthz != "ok":
                clues.append(f"Kubelet healthz failed on {diag.node_name}: {healthz[:100]}")
            if systemctl and systemctl != "active":
                clues.append(f"Kubelet not active on {diag.node_name}: {systemctl}")
        except Exception:
            pass
        return clues

    def _parse_disk_readiness_evidence(self, diag):
        """Parse lsblk + df output to detect NVMe initialization failures.

        On i4i instances, InvalidDiskCapacity events fire when the NVMe root
        volume hasn't finished setup. df showing 0 capacity confirms this.
        """
        clues = []
        r = getattr(diag, "resource_utilization", None)
        if not r or r.status != "Success" or not r.output:
            return clues
        if "nvme" not in r.output.lower() and "disk_readiness" not in r.output:
            return clues
        try:
            for line in r.output.split("\n"):
                parts = line.split()
                # df -h output: "Filesystem  Size  Used  Avail  Use%  Mounted on"
                if len(parts) >= 6 and parts[-1] == "/":
                    size = parts[1]
                    if size in ("0", "0B", "0K"):
                        clues.append(f"Root filesystem has 0 capacity on {diag.node_name} — NVMe not initialized")
                        break
        except Exception:
            pass
        return clues

    def _parse_mpstat_evidence(self, diag):
        """Parse mpstat per-core output to detect reservedSystemCPUs saturation.

        If any core has idle < 10%, it's saturated. If saturated cores are in
        the reserved range (typically 0-1), system processes are starved.

        mpstat output format (with AM/PM):
          01:00:01 AM  0  5.00  0.00  2.00  0.00  0.00  0.00  0.00  0.00  0.00  93.00
        mpstat output format (24h):
          01:00:01  0  5.00  0.00  2.00  0.00  0.00  0.00  0.00  0.00  0.00  93.00
        """
        clues = []
        r = getattr(diag, "resource_utilization", None)
        if not r or r.status != "Success" or not r.output:
            return clues
        if "NO_MPSTAT" in r.output:
            return clues
        try:
            saturated_cores = []
            for line in r.output.split("\n"):
                line = line.strip()
                if not line or line.startswith("Average:") or line.startswith("Linux"):
                    continue
                fields = line.split()
                if len(fields) < 11:
                    continue

                # Find the CPU column — skip time and optional AM/PM
                cpu_idx = 1
                if fields[1] in ("AM", "PM"):
                    cpu_idx = 2
                if cpu_idx >= len(fields):
                    continue

                core_str = fields[cpu_idx]
                if core_str in ("all", "CPU"):
                    continue
                try:
                    core_id = int(core_str)
                    idle_pct = float(fields[-1])  # last column is always %idle
                    if idle_pct < 10.0:
                        saturated_cores.append(core_id)
                except (ValueError, IndexError):
                    continue
            if saturated_cores:
                cores_str = ",".join(str(c) for c in saturated_cores[:5])
                clues.append(
                    f"CPU cores saturated (idle<10%) on {diag.node_name}: "
                    f"cores [{cores_str}]"
                    + (f" +{len(saturated_cores)-5} more" if len(saturated_cores) > 5 else "")
                )
        except Exception:
            pass
        return clues

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _count_warning_reasons(self, events):
        reasons = {}
        for e in events:
            if e.event_type == "Warning":
                reasons[e.reason] = reasons.get(e.reason, 0) + 1
        return reasons

    def _merge_targets(self, stuck, condition_problems):
        seen = set()
        result = []
        for name, iid in stuck:
            if name not in seen:
                seen.add(name)
                result.append((name, iid))
        for pn in condition_problems:
            if pn.node_name not in seen:
                seen.add(pn.node_name)
                result.append((pn.node_name, pn.instance_id))
        return result

    def _resolve_instance_id(self, node_name):
        try:
            v1 = self.k8s_client.CoreV1Api()
            node = v1.read_node(node_name)
            pid = node.spec.provider_id or ""
            return pid.rsplit("/", 1)[-1] if "/" in pid else pid
        except Exception:
            return ""
