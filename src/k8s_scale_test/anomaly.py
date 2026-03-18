"""Anomaly detection — evidence-driven investigation pipeline.

When the monitor detects a rate drop or timeout, this module investigates
by collecting evidence from every available layer and correlating it to
find a root cause. The investigation is structured as a pipeline of
layers, each adding more detail:

Investigation layers (in order):
    0. SharedContext — queries the shared in-memory context for scanner
       findings that temporally overlap the alert. Matched findings are
       referenced as prior evidence instead of re-collected.
    1. K8s Warning events — the cheapest signal; always collected first.
    2. Pod phase breakdown — distinguishes Pending (unschedulable) from
       ContainerCreating (scheduled but CNI/runtime issues).
    3. Stuck-pod nodes — identifies which nodes have Pending pods.
    4. Node conditions — K8s-reported problems (NotReady, MemoryPressure).
    4.5. AMP metric query — queries AMP/Prometheus for node-level metrics
         (CPU, memory, network errors, IPAMD, pod restarts) within a ±3 min
         window around the alert timestamp. Only runs if an AMPMetricCollector
         is configured.
    5. EC2 ENI/prefix state — checks VPC networking (IP exhaustion,
       IPAMD failures). Only runs if an AWS client is available.
    6. SSM node diagnostics — runs shell commands on problem nodes to
       collect low-level evidence (logs, PSI, disk, CPU). Commands are
       chosen dynamically based on what earlier layers found.

The pipeline is evidence-driven: each layer's output determines what
the next layer investigates. For example, FailedCreatePodSandBox events
trigger IPAMD log collection via SSM, while OOMKilling events trigger
memory/PSI checks.

No hardcoded failure-mode detectors — the correlation logic scans all
collected evidence for known patterns and assembles a root cause string.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable, Optional

from k8s_scale_test.diagnostics import NodeDiagnosticsCollector
from k8s_scale_test.evidence import EvidenceStore
from k8s_scale_test.health_sweep import NodeMetricResult, check_threshold
from k8s_scale_test.kb_matcher import SignatureMatcher
from k8s_scale_test.kb_store import KBStore
from k8s_scale_test.metrics import NodeMetricsAnalyzer
from k8s_scale_test.models import (
    Alert, Finding, K8sEvent, NodeDiagnostic, NodeMetric,
    ProblemNode, Severity, TestConfig,
)
from k8s_scale_test.shared_context import match_findings
from k8s_scale_test.tracing import span

log = logging.getLogger(__name__)


class AnomalyDetector:
    """Investigates alerts by collecting multi-layer evidence and correlating it.

    Parameters
    ----------
    config : TestConfig
        Test configuration (time windows, thresholds, KB settings).
    k8s_client :
        Kubernetes API client module.
    node_metrics : NodeMetricsAnalyzer
        Queries Prometheus for node-level metrics.
    node_diag : NodeDiagnosticsCollector
        Runs SSM commands on EC2 instances for low-level diagnostics.
    evidence_store : EvidenceStore
        Persists findings to disk.
    run_id : str
        Current test run identifier.
    operator_cb : callable, optional
        Async callback to notify the operator of unresolved findings.
    aws_client : optional
        Boto3 session for EC2 API calls (ENI/subnet queries).
    kb_store : KBStore, optional
        Known-issues knowledge base for pattern matching.
    kb_matcher : SignatureMatcher, optional
        Matches current events against known KB entries.
    amp_collector : AMPMetricCollector, optional
        Pre-configured AMP/Prometheus metric collector for querying
        node-level metrics during reactive investigations. When None,
        the AMP investigation layer is skipped.
    shared_ctx : SharedContext, optional
        In-memory shared context for cross-source correlation. When
        provided, the anomaly detector queries it for recent scanner
        findings before starting the investigation pipeline. When None,
        the SharedContext lookup is skipped entirely.
    """

    def __init__(
        self, config: TestConfig, k8s_client,
        node_metrics: NodeMetricsAnalyzer,
        node_diag: NodeDiagnosticsCollector,
        evidence_store: EvidenceStore, run_id: str,
        operator_cb: Optional[Callable[[str, dict], Awaitable[str]]] = None,
        aws_client=None,
        kb_store: Optional[KBStore] = None,
        kb_matcher: Optional[SignatureMatcher] = None,
        amp_collector=None,
        shared_ctx=None,
    ) -> None:
        self.config = config
        self.k8s_client = k8s_client
        self.node_metrics = node_metrics
        self.node_diag = node_diag
        self.evidence_store = evidence_store
        self.run_id = run_id
        self._operator_cb = operator_cb
        self.aws_client = aws_client
        self.kb_store = kb_store
        self.kb_matcher = kb_matcher or SignatureMatcher(threshold=config.kb_match_threshold)
        self.amp_collector = amp_collector
        self.shared_ctx = shared_ctx
        self._node_cache: dict[str, str] = {}  # node_name -> instance_id

    async def handle_alert(self, alert: Alert) -> Finding:
        """Run the full investigation pipeline for a single alert.

        Collects evidence from all layers (K8s events → pod phases → stuck
        nodes → node conditions → AMP metrics → ENI state → SSM diagnostics),
        then correlates everything into a Finding with a root cause (or None
        if inconclusive).

        If a Known Issues KB is configured, checks for matching patterns
        first — a KB hit short-circuits the full investigation.

        Parameters
        ----------
        alert : Alert
            The alert that triggered this investigation (rate drop, timeout, etc.).

        Returns
        -------
        Finding
            The investigation result, persisted to the evidence store.
        """
        log.info("Investigating: %s", alert.message)
        ns_list = alert.context.get("namespaces", ["default"])
        window = timedelta(minutes=self.config.event_time_window_minutes)

        with span("alert/" + alert.alert_type.value,
                   alert_id=str(alert)) as alert_span:

            # Layer 0: Fetch scanner findings from SharedContext (time-windowed)
            scanner_matches_raw: list = []
            if self.shared_ctx is not None:
                try:
                    with span("investigation/shared_context"):
                        scanner_matches_raw = await self.shared_ctx.query(
                            alert.timestamp,
                            window_seconds=self.config.correlation_window_seconds,
                        )
                except Exception as exc:
                    log.warning("SharedContext query failed, continuing without scanner findings: %s", exc)
                    scanner_matches_raw = []

            # Layer 1: K8s events — the starting point for any investigation
            with span("investigation/k8s_events"):
                events = await self._collect_k8s_events(ns_list, window)
            warning_reasons = self._count_warning_reasons(events)
            if warning_reasons:
                log.info("  Events: %s", ", ".join(f"{r}={c}" for r, c in
                         sorted(warning_reasons.items(), key=lambda x: -x[1])[:5]))

            # KB lookup — check known patterns before full investigation
            if self.kb_store is not None:
                matches = self.kb_matcher.match(events, [], self.kb_store.load_all())
                if matches:
                    best_entry, best_score = matches[0]
                    log.info("  KB match: %s (score=%.2f)", best_entry.entry_id, best_score)
                    now = datetime.now(timezone.utc)
                    finding = Finding(
                        finding_id=str(uuid.uuid4()),
                        timestamp=now,
                        severity=best_entry.severity,
                        symptom=alert.message,
                        affected_resources=[],
                        k8s_events=events,
                        node_metrics=[],
                        node_diagnostics=[],
                        evidence_references=[f"kb_match:{best_entry.entry_id}"],
                        root_cause=best_entry.root_cause,
                        resolved=True,
                    )
                    self.kb_store.update_occurrence(best_entry.entry_id, now)
                    self.evidence_store.save_finding(self.run_id, finding)
                    if alert_span is not None:
                        alert_span.set_attribute("root_cause", best_entry.root_cause or "")
                        alert_span.set_attribute("severity", best_entry.severity.value)
                    return finding

            # Layer 2: Pod phase breakdown — distinguish Pending vs ContainerCreating
            with span("investigation/pod_phases"):
                phase_breakdown = await self._get_pod_phase_breakdown(ns_list)
            if phase_breakdown:
                log.info("  Pod phases: %s", ", ".join(f"{k}={v}" for k, v in phase_breakdown.items() if v > 0))

            # Layer 3: Nodes with stuck pods (scheduled but not running)
            with span("investigation/stuck_nodes"):
                stuck_nodes = await self._find_stuck_pod_nodes(ns_list)

            # Layer 4: Standard node health (conditions)
            condition_problems = await self.node_metrics.identify_problem_nodes()

            # Merge investigation targets
            targets = self._merge_targets(stuck_nodes, condition_problems)
            if targets:
                log.info("  Investigating %d nodes (%d stuck-pod, %d bad-condition)",
                         len(targets), len(stuck_nodes), len(condition_problems))

            # Layer 0 continued: Match scanner findings against alert resources
            scanner_matches: list = []
            if scanner_matches_raw:
                alert_resources: set[str] = set()
                alert_resources.update(alert.context.get("namespaces", []))
                alert_resources.update(n for n, _ in stuck_nodes)
                alert_resources.update(e.involved_object_name for e in events)
                scanner_matches = match_findings(scanner_matches_raw, alert, alert_resources)
                if scanner_matches:
                    log.info("  SharedContext: %d scanner matches (%d strong)",
                             len(scanner_matches),
                             sum(1 for _, _, m in scanner_matches if m == "strong"))

            # Layer 4.5: AMP metrics (if configured)
            amp_metrics: dict[str, list[NodeMetricResult]] = {}
            if self.amp_collector is not None:
                with span("investigation/amp_metrics"):
                    amp_metrics = await self._collect_amp_metrics(alert.timestamp)

            # Layer 5: EC2 ENI/prefix state (if AWS client available)
            eni_evidence = {}
            if self.aws_client and targets:
                with span("investigation/eni"):
                    eni_evidence = await self._collect_eni_state(targets[:10])

            # Layer 6: SSM — dynamic commands based on what events tell us
            extra_cmds = self._pick_ssm_commands(warning_reasons, eni_evidence)
            diags: list[NodeDiagnostic] = []
            with span("investigation/ssm"):
                for node_name, iid in targets[:3]:
                    if iid:
                        diags.append(await self.node_diag.collect(node_name, iid,
                                     extra_commands=extra_cmds))

            # Correlate everything
            metrics = [pn.metrics for pn in condition_problems]
            finding = self._correlate(alert, events, metrics, diags,
                                      stuck_nodes, eni_evidence, phase_breakdown,
                                      amp_metrics=amp_metrics,
                                      scanner_matches=scanner_matches)
            self.evidence_store.save_finding(self.run_id, finding)

            # Set root cause and severity on the parent alert span
            if alert_span is not None:
                alert_span.set_attribute("root_cause", finding.root_cause or "")
                alert_span.set_attribute("severity", finding.severity.value)

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
        """Collect Warning events from K8s API (Layer 1).

        Events are cleared before each test run (by the controller), so
        everything returned here is from the current run — no time
        filtering needed.

        Paginates with limit=200 per page to handle high-volume clusters
        where thousands of Warning events can accumulate during scaling.

        Parameters
        ----------
        namespaces : list[str]
            Namespaces to query.
        time_window : timedelta
            Not currently used for filtering (events are pre-cleared),
            but kept for future use.

        Returns
        -------
        list[K8sEvent]
            All Warning events across the given namespaces.
        """
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

    async def _collect_amp_metrics(
        self, alert_timestamp: datetime,
    ) -> dict[str, list[NodeMetricResult]]:
        """Query AMP for node-level metrics around the alert timestamp (±3 min).

        Delegates to :pymethod:`AMPMetricCollector.collect_all` which runs
        all PromQL queries concurrently.

        Returns dict keyed by metric category (cpu, memory, network_errors,
        ipamd, pod_restarts).  Returns empty dict if ``amp_collector`` is
        ``None`` or all queries fail.
        """
        if self.amp_collector is None:
            return {}
        try:
            return await self.amp_collector.collect_all()
        except Exception as exc:
            log.warning("AMP metric collection failed: %s", exc)
            return {}


    async def _get_pod_phase_breakdown(self, namespaces):
        """Get pod phase counts from Deployment status objects (Layer 2).

        Uses Deployment .status.replicas and .status.readyReplicas to
        infer Pending vs Running counts. This is much cheaper than listing
        all pods individually — a single list_namespaced_deployment call
        covers hundreds of deployments.

        Returns
        -------
        dict[str, int]
            Counts keyed by phase name (Pending, Running, etc.).
        """
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
        """Find nodes that have Pending pods assigned to them (Layer 3).

        A pod that is Pending *and* has a node_name means it was scheduled
        but something on that node is preventing it from starting (CNI,
        disk, kubelet issues). This is different from unschedulable pods
        (which have no node_name).

        Limits to 200 pods per namespace and returns the top 20 nodes
        by stuck-pod count to keep the response manageable.

        Returns
        -------
        list[tuple[str, str]]
            List of ``(node_name, ec2_instance_id)`` pairs, sorted by
            stuck-pod count descending.
        """
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
        """Query EC2 ENI and prefix-delegation state for problem nodes (Layer 5).

        For each node, checks:
        - How many ENIs are attached (eni_count)
        - How many IPv4 prefixes are delegated (prefix_count) — zero means
          the VPC CNI's IPAMD has no IP addresses to assign to pods
        - Subnet available IPs — low counts indicate IP exhaustion in the AZ

        Runs in executor threads to avoid blocking the event loop, since
        each EC2 API call can take 100-500ms.

        Parameters
        ----------
        nodes : list[tuple[str, str]]
            List of ``(node_name, instance_id)`` pairs to check.

        Returns
        -------
        dict[str, dict]
            Keyed by node_name, values contain eni_count, prefix_count,
            subnet_id, az, and subnet_available_ips.
        """
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
        """Choose extra SSM commands based on what events and EC2 data revealed.

        This is the "dynamic command selection" — instead of always running
        every possible diagnostic, we only run commands that are relevant to
        the symptoms we've already observed. This matters because each command
        adds an SSM send_command call per investigated node (up to 3 nodes),
        and SSM calls are slow (~5s each).

        The logic follows a simple pattern:
            IF we saw event X → THEN collect diagnostic Y

        Parameters
        ----------
        warning_reasons : dict[str, int]
            Event reason → count mapping from K8s Warning events.
        eni_evidence : dict[str, dict]
            Per-node ENI/prefix data from ``_collect_eni_state``.

        Returns
        -------
        dict[str, str]
            Label → shell command mapping to pass to SSM.
        """
        extra = {}
        # Check if any node has zero IPv4 prefixes — indicates IPAMD failure
        zero_pfx = any(v.get("prefix_count", -1) == 0 for v in eni_evidence.values())

        # --- CNI / IPAMD checks ---
        # FailedCreatePodSandBox means the CNI plugin couldn't set up networking.
        # Zero prefixes confirms the IPAMD has no IPs to hand out.
        # Either symptom justifies pulling IPAMD logs and CNI config.
        if warning_reasons.get("FailedCreatePodSandBox", 0) > 0 or zero_pfx:
            extra["ipamd_log"] = "tail -200 /var/log/aws-routed-eni/ipamd.log 2>/dev/null || echo NO_IPAMD_LOG"
            extra["cni_config"] = "cat /etc/cni/net.d/* 2>/dev/null | head -50"
            # Count network interfaces — high counts (>200) correlate with
            # netlink dump interruption during MAC generation
            extra["iface_count"] = (
                "echo IFACE_COUNT=$(ip link show | grep -c '^[0-9]'); "
                "echo VETH_COUNT=$(ip link show type veth | grep -c '^[0-9]'); "
                "ip link show type veth 2>/dev/null | head -40"
            )
            # BPF trace for kernel-level netlink/veth diagnostics.
            # Runs for 30s capturing register_netdevice rate, netlink_dump
            # timing, and veth_newlink calls. Only runs if bpftrace is
            # installed (pre-loaded via EC2NodeClass userData).
            # Output is JSON on stdout, histogram on stderr.
            extra["bpf_netlink_trace"] = (
                "if command -v bpftrace >/dev/null 2>&1; then "
                "timeout 35 bpftrace -e '"
                "BEGIN { @start=nsecs; @ev=0; printf(\"{\\\"events\\\":[\"); } "
                "kprobe:register_netdevice { @reg=count(); @reg_s=count(); } "
                "kprobe:unregister_netdevice { @unreg=count(); } "
                "kprobe:veth_newlink { @vnl=count(); @vnl_s=count(); } "
                "kprobe:netlink_dump { @dstart[tid]=nsecs; @dcnt=count(); } "
                "kretprobe:netlink_dump { $s=@dstart[tid]; if($s>0){@dhist=hist((nsecs-$s)/1000);delete(@dstart[tid]);} } "
                "kprobe:rtnl_dump_ifinfo { @ldump=count(); } "
                "interval:s:1 { $e=(nsecs-@start)/1000000000; if(@ev>0){printf(\",\");} "
                "printf(\"{\\\"t\\\":%lld,\\\"reg\\\":%lld,\\\"vnl\\\":%lld}\",$e,@reg_s,@vnl_s); "
                "@ev++; clear(@reg_s); clear(@vnl_s); } "
                "interval:s:30 { printf(\"],\\\"summary\\\":{\\\"register_netdevice\\\":%lld,"
                "\\\"unregister_netdevice\\\":%lld,\\\"veth_newlink\\\":%lld,"
                "\\\"netlink_dumps\\\":%lld,\\\"rtnl_dump_ifinfo\\\":%lld}}\","
                "@reg,@unreg,@vnl,@dcnt,@ldump); exit(); } "
                "END { clear(@dstart); clear(@start); clear(@ev); }' 2>/dev/null; "
                "else echo NO_BPFTRACE; fi"
            )

        # --- Disk / image checks ---
        # "Failed" and "ErrImagePull" suggest container image pull failures,
        # which can be caused by full disks or registry auth issues.
        if warning_reasons.get("Failed", 0) > 0 or warning_reasons.get("ErrImagePull", 0) > 0:
            extra["disk_images"] = "df -h; echo '---'; crictl images 2>/dev/null | tail -20"

        # --- Memory checks ---
        # Evictions and OOM kills indicate memory pressure. PSI (Pressure
        # Stall Information) from /proc/pressure/ confirms whether the kernel
        # is actively stalling processes due to memory contention.
        if warning_reasons.get("Evicted", 0) > 0 or warning_reasons.get("OOMKilling", 0) > 0:
            extra["memory"] = "cat /proc/meminfo | head -10; echo '---'; dmesg | grep -i oom | tail -10"
            extra["psi"] = (
                "cat /proc/pressure/cpu 2>/dev/null; echo '===PSI_SEP==='; "
                "cat /proc/pressure/memory 2>/dev/null; echo '===PSI_SEP==='; "
                "cat /proc/pressure/io 2>/dev/null"
            )

        # --- Scheduling checks ---
        # FailedScheduling means the scheduler couldn't place pods. Kubelet
        # config (maxPods, reservations) may be misconfigured.
        if warning_reasons.get("FailedScheduling", 0) > 0:
            extra["kubelet_cfg"] = "cat /etc/kubernetes/kubelet/kubelet-config.json 2>/dev/null | head -30"

        # --- Node readiness checks ---
        # NodeNotReady can be caused by kubelet crashes or TLS bootstrap
        # delays (common during rapid scale-up when many nodes join at once).
        if warning_reasons.get("NodeNotReady", 0) > 0:
            extra["kubelet_status"] = "systemctl status kubelet --no-pager -l 2>&1 | tail -20"
            extra["kubelet_health"] = (
                "curl -sk https://localhost:10250/healthz 2>&1; echo '===HEALTH_SEP==='; "
                "systemctl is-active kubelet 2>&1"
            )

        # InvalidDiskCapacity fires on i4i instances when the NVMe root
        # volume hasn't finished initializing during boot. lsblk + df
        # confirms whether the volume is actually mounted.
        if warning_reasons.get("InvalidDiskCapacity", 0) > 0:
            extra["disk_readiness"] = "lsblk 2>/dev/null | grep nvme; echo '---'; df -h / 2>/dev/null"

        # --- Per-core CPU check ---
        # mpstat takes 1 second to sample, so only run it when we suspect
        # CPU saturation. Two triggers:
        # 1. Eviction events (CPU pressure can cause evictions)
        # 2. We have warning events but none of the specific checks above
        #    matched — something is wrong but we don't know what, so check CPU.
        total_warnings = sum(warning_reasons.values())
        if (warning_reasons.get("Evicted", 0) > 0
                or (total_warnings > 0 and not extra)):
            extra["mpstat"] = "mpstat -P ALL 1 1 2>/dev/null | tail -n +4 || echo NO_MPSTAT"

        return extra

    # ------------------------------------------------------------------
    # Correlation — evidence-driven
    # ------------------------------------------------------------------

    def _correlate(self, alert, events, metrics, diagnostics,
                   stuck_nodes, eni_evidence, phase_breakdown,
                   amp_metrics=None, scanner_matches=None):
        """Combine all collected evidence into a single Finding.

        Builds an evidence_references list (human-readable summary strings),
        determines severity from evidence weight, and attempts to extract
        a root cause from all layers.

        Parameters
        ----------
        alert : Alert
            The original alert that triggered investigation.
        events : list[K8sEvent]
            All collected K8s Warning events.
        metrics : list[NodeMetric]
            Node-level metrics from problem nodes.
        diagnostics : list[NodeDiagnostic]
            SSM command outputs from investigated nodes.
        stuck_nodes : list[tuple[str, str]]
            Nodes with Pending pods.
        eni_evidence : dict
            Per-node ENI/prefix data.
        phase_breakdown : dict
            Pod phase counts.
        amp_metrics : dict[str, list[NodeMetricResult]] | None
            AMP metric results keyed by category. None when AMP is not
            configured or collection failed.
        scanner_matches : list[tuple[datetime, ScanResult, str]] | None
            Matched scanner findings from SharedContext correlation.
            Each tuple is (timestamp, ScanResult, match_type) where
            match_type is "strong" or "weak". None when SharedContext
            is not configured or query returned no matches.

        Returns
        -------
        Finding
            Complete investigation result.
        """
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

        # AMP metric evidence
        if amp_metrics:
            amp_issues = []
            for category, results in amp_metrics.items():
                for r in results:
                    issue = check_threshold(r)
                    if issue:
                        amp_issues.append(issue)
            if amp_issues:
                evidence_refs.append(f"amp:{len(amp_issues)}_violations")
            else:
                evidence_refs.append(
                    f"amp:checked={sum(len(v) for v in amp_metrics.values())},no_violations"
                )
        elif self.amp_collector is not None:
            evidence_refs.append("amp:no_data")

        # Scanner correlation evidence
        if scanner_matches:
            strong = [m for m in scanner_matches if m[2] == "strong"]
            weak = [m for m in scanner_matches if m[2] == "weak"]
            evidence_refs.append(f"scanner_correlation:{len(strong)}_strong,{len(weak)}_weak")
            for ts, result, match_type in scanner_matches[:5]:
                evidence_refs.append(f"scanner:{result.query_name}({match_type})")

        # Determine severity from evidence weight
        severity = self._assess_severity(warnings, stuck_nodes, eni_evidence)

        # Extract root cause from all evidence
        root_cause = self._extract_root_cause(
            warning_reasons, eni_evidence, diagnostics, phase_breakdown,
            amp_metrics=amp_metrics, scanner_matches=scanner_matches)

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
        """Determine finding severity from evidence weight.

        Severity escalation rules:
        - CRITICAL: any node has zero prefixes (complete networking failure),
          OR >10 stuck nodes, OR >50 warning events.
        - WARNING: any stuck nodes exist, OR >10 warning events.
        - INFO: everything else (minor or transient issues).

        Returns
        -------
        Severity
        """
        zero_pfx = sum(1 for v in eni_evidence.values() if v.get("prefix_count", -1) == 0)
        if zero_pfx > 0:
            return Severity.CRITICAL
        if len(stuck_nodes) > 10 or len(warnings) > 50:
            return Severity.CRITICAL
        if stuck_nodes or len(warnings) > 10:
            return Severity.WARNING
        return Severity.INFO

    def _extract_root_cause(self, warning_reasons, eni_evidence,
                            diagnostics, phase_breakdown,
                            amp_metrics=None, scanner_matches=None):
        """Scan all collected evidence for root cause patterns.

        Checks evidence in priority order (most common scale-test failures first):
        1. Karpenter / capacity issues (InsufficientCapacityError, etc.)
        2. Pod sandbox / CNI failures (FailedCreatePodSandBox)
        3. ENI / prefix exhaustion (zero prefixes, subnet IP exhaustion)
        4. SSM log patterns (IPAMD empty datastore, API throttling, OOM)
        5. Proactive node health (PSI, kubelet, disk, CPU saturation)
        6. Phase-based clues (large Pending or ContainerCreating counts)
        7. AMP metric evidence (CPU > 90%, memory > 90%, network errors,
           pod restarts > 5)
        8. Scanner correlation clues (WARNING/CRITICAL scanner findings
           pre-detected by the ObservabilityScanner)

        Returns None if no pattern matches — the finding is "unresolved"
        and the operator is notified.

        Parameters
        ----------
        warning_reasons : dict[str, int]
            Event reason → count from K8s Warning events.
        eni_evidence : dict
            Per-node ENI/prefix data.
        diagnostics : list[NodeDiagnostic]
            SSM command outputs.
        phase_breakdown : dict
            Pod phase counts.
        amp_metrics : dict[str, list[NodeMetricResult]] | None
            AMP metric results keyed by category. None when AMP is not
            configured or collection failed.
        scanner_matches : list[tuple[datetime, ScanResult, str]] | None
            Matched scanner findings from SharedContext correlation.
            WARNING/CRITICAL findings are added as "Scanner pre-detected:"
            clues. None when SharedContext is not configured.

        Returns
        -------
        str | None
            Semicolon-separated root cause string, or None if inconclusive.
        """
        clues = []

        # --- Karpenter / capacity issues (most common at scale) ---
        # InsufficientCapacityError means EC2 couldn't launch the requested
        # instance type — either spot capacity is exhausted or the NodePool
        # CPU limit has been reached.
        ice = warning_reasons.get("InsufficientCapacityError", 0)
        if ice > 0:
            clues.append(f"InsufficientCapacityError x{ice} — Karpenter cannot provision nodes "
                         f"(spot exhaustion or NodePool limit reached)")

        idc = warning_reasons.get("InvalidDiskCapacity", 0)
        if idc > 0:
            # InvalidDiskCapacity is usually transient on i4i instances — the
            # NVMe root volume takes a few seconds to initialize during boot.
            # Only flag it as a root cause if it persists (>50 events) AND
            # there are no InsufficientCapacityError events (which would be
            # the real problem, with disk errors as a side effect).
            if ice == 0 and idc > 50:
                clues.append(f"InvalidDiskCapacity x{idc} — persistent disk setup failures")

        tgpe = warning_reasons.get("TerminationGracePeriodExpiring", 0)
        if tgpe > 0 and ice > 0:
            # TerminationGracePeriodExpiring events appear alongside
            # InsufficientCapacityError — they're Karpenter cleaning up
            # NodeClaims that failed to provision. Not a separate issue.
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

        # --- AMP metric evidence ---
        if amp_metrics:
            for category, results in amp_metrics.items():
                for r in results:
                    if category == "cpu" and r.value > 90:
                        clues.append(f"AMP: CPU {r.value:.1f}% on {r.node_name}")
                    elif category == "memory" and r.value > 90:
                        clues.append(f"AMP: Memory {r.value:.1f}% on {r.node_name}")
                    elif category == "network_errors" and r.value > 0:
                        clues.append(f"AMP: Network errors {r.value:.1f}/s on {r.node_name}")
                    elif category == "pod_restarts" and r.value > 5:
                        clues.append(f"AMP: {r.value:.0f} pod restarts on {r.node_name}")

        # --- Scanner correlation clues (WARNING/CRITICAL findings only) ---
        if scanner_matches:
            for ts, result, match_type in scanner_matches:
                if result.severity.value in ("warning", "critical"):
                    clues.append(f"Scanner pre-detected: {result.title}")

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
        """Tally Warning events by reason. Returns ``{reason: count}``."""
        reasons = {}
        for e in events:
            if e.event_type == "Warning":
                reasons[e.reason] = reasons.get(e.reason, 0) + 1
        return reasons

    def _merge_targets(self, stuck, condition_problems):
        """Combine stuck-pod nodes and condition-problem nodes into a deduplicated list.

        Stuck-pod nodes come first (they're the primary investigation targets),
        followed by nodes with bad K8s conditions that weren't already in the
        stuck list.

        Returns
        -------
        list[tuple[str, str]]
            Deduplicated ``(node_name, instance_id)`` pairs.
        """
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
        """Look up the EC2 instance ID for a K8s node name.

        Reads the node's ``spec.providerID`` (format: ``aws:///az/i-xxx``)
        and extracts the instance ID from the last path segment.

        Returns
        -------
        str
            EC2 instance ID, or empty string if lookup fails.
        """
        try:
            v1 = self.k8s_client.CoreV1Api()
            node = v1.read_node(node_name)
            pid = node.spec.provider_id or ""
            return pid.rsplit("/", 1)[-1] if "/" in pid else pid
        except Exception:
            return ""
