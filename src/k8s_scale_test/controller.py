"""Scale Test Controller — orchestrates the full test lifecycle."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
from datetime import datetime, timezone
from typing import Optional

from k8s_scale_test.agent_context import ContextFileWriter
from k8s_scale_test.anomaly import AnomalyDetector
from k8s_scale_test.reviewer import FindingReviewer
from k8s_scale_test.cl2_parser import CL2ResultParser
from k8s_scale_test.diagnostics import NodeDiagnosticsCollector
from k8s_scale_test.evidence import EvidenceStore
from k8s_scale_test.events import EventWatcher
from k8s_scale_test.flux import FluxRepoReader, FluxRepoWriter
from k8s_scale_test.health_sweep import HealthSweepAgent
from k8s_scale_test.infra_health import InfraHealthAgent
from k8s_scale_test.kb_ingest import IngestionPipeline
from k8s_scale_test.kb_matcher import SignatureMatcher
from k8s_scale_test.kb_store import KBStore
from k8s_scale_test.metrics import NodeMetricsAnalyzer
from k8s_scale_test.models import (
    Alert,
    AlertType,
    CL2ParseError,
    CL2Summary,
    CL2TestStatus,
    Finding,
    GoNoGo,
    PreflightReport,
    RunValidity,
    ScalingResult,
    ScalingStep,
    Severity,
    TestConfig,
    TestRunSummary,
)
from k8s_scale_test.monitor import PodRateMonitor
from k8s_scale_test.observability import ObservabilityScanner, Phase as ScanPhase, ScanResult
from k8s_scale_test.preflight import PreflightChecker
from k8s_scale_test.tracing import phase_span, install_slow_callback_monitor

import time as _time

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scanner executor factories — create the callables that ObservabilityScanner
# needs without modifying any existing module.
# ---------------------------------------------------------------------------

def _make_prometheus_executor(config) -> "Callable[[str], Awaitable[dict]] | None":
    """Create a PromQL executor from config, or return None if AMP/Prometheus not configured."""
    if not config.amp_workspace_id and not getattr(config, "prometheus_url", None):
        return None

    from k8s_scale_test.health_sweep import AMPMetricCollector

    collector = AMPMetricCollector(
        amp_workspace_id=config.amp_workspace_id,
        prometheus_url=getattr(config, "prometheus_url", None),
        aws_profile=getattr(config, "aws_profile", None),
    )

    async def executor(query: str) -> dict:
        return await collector._query_promql(query)

    return executor


def _make_cloudwatch_executor(config, aws_client) -> "Callable[[str, str, str], Awaitable[dict]] | None":
    """Create a CloudWatch Logs Insights executor, or return None if not configured."""
    if not config.cloudwatch_log_group or not config.eks_cluster_name:
        return None
    if aws_client is None:
        return None

    async def executor(query: str, start_time: str, end_time: str) -> dict:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, _run_cw_insights_query,
            aws_client, config.cloudwatch_log_group, query, start_time, end_time,
        )
        return result

    return executor


def _run_cw_insights_query(
    aws_client, log_group: str, query: str, start_time: str, end_time: str,
) -> dict:
    """Blocking CloudWatch Logs Insights query — runs inside ``run_in_executor``."""
    import time
    from datetime import datetime as _dt, timezone as _tz

    cw = aws_client.client("logs")
    start_epoch = int(_dt.fromisoformat(start_time).timestamp())
    end_epoch = int(_dt.fromisoformat(end_time).timestamp())

    resp = cw.start_query(
        logGroupName=log_group,
        startTime=start_epoch,
        endTime=end_epoch,
        queryString=query,
    )
    query_id = resp["queryId"]

    # Poll until complete (max ~30s)
    for _ in range(30):
        result = cw.get_query_results(queryId=query_id)
        if result["status"] in ("Complete", "Failed", "Cancelled"):
            break
        time.sleep(1)

    return {
        "status": result.get("status"),
        "results": [
            {field["field"]: field["value"] for field in row}
            for row in result.get("results", [])
        ],
    }


class _ObserverHandle:
    """Compatibility wrapper so _stop_observer works with both subprocess.Popen and thread-based observers."""

    def __init__(self, thread):
        self._thread = thread

    def terminate(self):
        pass  # Thread stops when self._running is set to False

    def wait(self, timeout=5):
        self._thread.join(timeout=timeout)

    def kill(self):
        pass


class ScaleTestController:
    """Orchestrates preflight, scaling, monitoring, and anomaly detection."""

    def __init__(
        self, config: TestConfig, evidence_store: EvidenceStore,
        k8s_client=None, aws_client=None,
    ) -> None:
        self.config = config
        self.evidence_store = evidence_store
        self.k8s_client = k8s_client
        self.aws_client = aws_client
        self._current_step = 0
        self._findings: list[Finding] = []
        self._paused = False
        self._namespaces: list[str] = []
        self._health_sweep: dict = {}
        self._karpenter_health: dict = {}
        self._ctx_writer: ContextFileWriter | None = None
        self._running = True  # Used by observer thread to know when to stop
        self._scanner: ObservabilityScanner | None = None
        self._scanner_task: asyncio.Task | None = None
        self._scanner_findings: list[ScanResult] = []
        self._shared_ctx: SharedContext | None = None
        self._reviewer: FindingReviewer | None = None
        self._review_tasks: list[asyncio.Task] = []

    async def run(self) -> TestRunSummary:
        """Execute the full test lifecycle.

        Phases (matching docs/test-lifecycle.md):
            1. Preflight — validate cluster capacity can support the target
            2. Operator Approval — human gate before scaling begins
            3. Infrastructure Scaling — deploy iperf3 servers, patch manifests
            4. Stressor Scaling — distribute pods, commit to Flux, monitor
            5. Hold at Peak — wait at target count, run health sweep
            6. Cleanup — reset replicas to 0, drain nodes
            7. Summary — generate report, cross-validate data, produce chart
        """
        start = datetime.now(timezone.utc)
        run_id = self.evidence_store.create_run(self.config)
        self._evidence_run_id = run_id
        log.info("Test run %s started", run_id)

        # Install slow-callback monitor for event loop health tracking
        loop = asyncio.get_event_loop()
        install_slow_callback_monitor(loop)

        # ── Phase 1: Preflight ──────────────────────────────────────────
        # Validate that the cluster has enough capacity (IPs, vCPU quota,
        # NodePool limits, pod ceiling) to support the target pod count.
        # If CL2 preload is configured, temporarily inflate the target to
        # account for the extra pods CL2 will create.
        cl2_extra_pods = 0
        if self.config.cl2_preload:
            from k8s_scale_test.models import CL2PreloadPlan
            if self.config.cl2_params:
                p = self.config.cl2_params
                _plan = CL2PreloadPlan(
                    target_pods=self.config.target_pods,
                    namespaces=int(p.get("NAMESPACES", "10")),
                    deployments_per_ns=int(p.get("DEPLOYMENTS_PER_NS", "5")),
                    pod_replicas=int(p.get("POD_REPLICAS", "2")),
                    services_per_ns=int(p.get("SERVICES_PER_NS", "5")),
                    configmaps_per_ns=int(p.get("CONFIGMAPS_PER_NS", "10")),
                    secrets_per_ns=int(p.get("SECRETS_PER_NS", "5")),
                )
            else:
                _plan = CL2PreloadPlan.from_target_pods(self.config.target_pods)
            cl2_extra_pods = _plan.total_pods
            log.info("Preflight: including %d CL2 preload pods (%d total with %d target)",
                     cl2_extra_pods, self.config.target_pods + cl2_extra_pods, self.config.target_pods)
            # Temporarily inflate target_pods for preflight capacity check
            original_target = self.config.target_pods
            self.config.target_pods = original_target + cl2_extra_pods

        with phase_span("preflight", run_id=run_id, target_pods=self.config.target_pods):
            report = await self._run_preflight()

        # Restore original target after preflight
        if cl2_extra_pods > 0:
            self.config.target_pods = original_target

        self.evidence_store.save_preflight_report(run_id, report)

        if report.decision.decision == GoNoGo.NO_GO:
            log.warning("Preflight NO_GO: %s", report.decision.blocking_constraints)
            await self._prompt_operator(
                "Preflight returned NO_GO. Review the report.", {"report": "preflight.json"}
            )

        # ── Phase 1b: Observability Check ───────────────────────────────
        # If any observability backends failed their preflight check, warn
        # the operator. Investigation capability will be degraded.
        if report.obs_total > 0 and report.obs_passed < report.obs_total:
            failed = report.obs_total - report.obs_passed
            obs_response = await self._prompt_operator(
                f"Observability: {failed}/{report.obs_total} checks failed — "
                f"investigation capability will be degraded. Continue or abort?",
                {"obs_passed": report.obs_passed, "obs_total": report.obs_total},
            )
            if obs_response and obs_response.lower() in ("abort", "cancel"):
                return self._make_summary(run_id, start, report, ScalingResult(
                    steps=[], total_pods_requested=0, total_pods_ready=0,
                    total_nodes_provisioned=0, peak_ready_rate=0.0,
                    completed=False, halt_reason="operator_aborted_observability",
                ))

        # ── Phase 2: Operator Approval ──────────────────────────────────
        approval = await self._prompt_operator(
            "Preflight complete. Approve to proceed with scaling?",
            {"max_achievable": report.max_achievable_pods, "target": self.config.target_pods},
        )
        if approval and approval.lower() in ("no", "abort", "cancel"):
            return self._make_summary(run_id, start, report, ScalingResult(
                steps=[], total_pods_requested=0, total_pods_ready=0,
                total_nodes_provisioned=0, peak_ready_rate=0.0,
                completed=False, halt_reason="operator_cancelled",
            ))

        # ── Phase 2a: CL2 Preload (optional, concurrent) ────────────────
        # ClusterLoader2 creates additional K8s objects (deployments, services,
        # configmaps, secrets) to simulate a realistic cluster. Runs in a
        # background task so pod scaling can proceed in parallel.
        cl2_task = None
        if self.config.cl2_preload:
            log.info("CL2 preload: launching concurrently with pod scaling")
            cl2_task = asyncio.create_task(self._run_cl2_preload())

        # ── Phase 3: Infrastructure Scaling ─────────────────────────────
        # Discover Flux-managed deployments, apply role-based filtering,
        # patch resource requests from preflight sizing, and scale
        # infrastructure services (iperf3 servers) before stressors.
        reader = FluxRepoReader(self.config.flux_repo_path)
        writer = FluxRepoWriter(self.config.flux_repo_path)
        deployments = reader.get_deployments()
        # Filter out excluded apps
        if self.config.exclude_apps:
            excluded = set(self.config.exclude_apps)
            deployments = [d for d in deployments if d.name not in excluded]
        # If include_apps is set, only keep those
        if self.config.include_apps:
            included = set(self.config.include_apps)
            deployments = [d for d in deployments if d.name in included]

        # 3a. Patch manifest resource requests from preflight sizing (Task 9.1)
        if report.stressor_sizing:
            sizing = report.stressor_sizing
            cpu_req = f"{sizing.cpu_request_millicores}m"
            mem_req = f"{sizing.memory_request_mi}Mi"
            cpu_lim = f"{int(sizing.cpu_request_millicores * sizing.cpu_limit_multiplier)}m"
            mem_lim = f"{int(sizing.memory_request_mi * sizing.memory_limit_multiplier)}Mi"
            log.info("Patching stressor manifests: requests=%s/%s, limits=%s/%s",
                     cpu_req, mem_req, cpu_lim, mem_lim)
            for d in deployments:
                if d.role == "stressor":
                    writer.set_resource_requests(
                        d.name, cpu_req, mem_req, cpu_lim, mem_lim, d.source_path
                    )

        # 3b. Separate deployments by role (Task 9.2)
        stressors = [d for d in deployments if d.role == "stressor"]
        infra = [d for d in deployments if d.role == "infrastructure"]
        unlabeled = [d for d in deployments if d.role is None]
        if unlabeled:
            names = ", ".join(d.name for d in unlabeled)
            log.info("Skipping %d unlabeled deployments (no scale-test/role): %s", len(unlabeled), names)

        # Use all labeled deployments for namespace tracking
        all_labeled = stressors + infra
        namespaces = list({d.namespace for d in all_labeled})
        self._namespaces = namespaces

        if not stressors:
            log.warning("No stressor deployments found after filtering")

        # 3c. Scale infrastructure and stressors together (single git push)
        # iperf3 clients have a readiness probe that checks server connectivity,
        # so they won't report Ready until servers are reachable. No need to
        # stage the deployment.
        if infra:
            server_count = max(1, self.config.target_pods // self.config.iperf3_server_ratio)
            infra_targets = [(d.name, server_count, d.source_path) for d in infra]
            writer.set_replicas_batch(infra_targets)
            log.info("Scaling %d infrastructure deployments to %d replicas each",
                     len(infra), server_count)

        # 3d. Weighted or even distribution for stressors (Task 9.4)
        if self.config.stressor_weights and stressors:
            targets = writer.distribute_pods_weighted(
                stressors, self.config.target_pods, self.config.stressor_weights
            )
            log.info("Weighted distribution of %d pods across %d stressors:", self.config.target_pods, len(stressors))
            for name, count, _ in targets:
                w = self.config.stressor_weights.get(name, 0.0)
                log.info("  %s: weight=%.2f, replicas=%d", name, w, count)
        elif stressors:
            targets = writer.distribute_pods_across_deployments(
                stressors, self.config.target_pods,
            )
            log.info("Even distribution of %d pods across %d stressors", self.config.target_pods, len(targets))
        else:
            targets = []

        # ── Phase 4: Clean Slate ───────────────────────────────────────
        # Delete existing K8s events so the anomaly detector only sees
        # events from this test run.
        await self._delete_events(namespaces)

        # ── Phase 5: Monitoring Setup ─────────────────────────────────
        # Wire up the monitoring pipeline: PodRateMonitor (watch-based
        # rate tracking), AnomalyDetector (alert investigation), EventWatcher
        # (event persistence), and HealthSweepAgent (peak-load node checks).
        monitor = PodRateMonitor(self.config, self.k8s_client, self.evidence_store, run_id, namespaces)
        self._monitor = monitor
        node_metrics = NodeMetricsAnalyzer(self.config, self.k8s_client, self.config.prometheus_url)
        node_diag = NodeDiagnosticsCollector(self.config, self.aws_client, self.evidence_store, run_id)

        # 5b. Initialize KB store and matcher if configured
        kb_store: KBStore | None = None
        kb_matcher: SignatureMatcher | None = None
        if self.config.kb_s3_bucket:
            log.info("Initializing Known Issues KB (table=%s, bucket=%s)",
                     self.config.kb_table_name, self.config.kb_s3_bucket)
            try:
                kb_store = KBStore(
                    self.aws_client,
                    table_name=self.config.kb_table_name,
                    s3_bucket=self.config.kb_s3_bucket,
                    s3_prefix=self.config.kb_s3_prefix,
                )
                kb_matcher = SignatureMatcher(threshold=self.config.kb_match_threshold)
                log.info("KB loaded %d active entries into cache", len(kb_store.load_all()))
            except Exception as exc:
                log.warning("KB initialization failed, proceeding without KB: %s", exc)
                kb_store = None
                kb_matcher = None

        # Create AMP collector for anomaly detector (if configured)
        amp_collector = None
        if self.config.amp_workspace_id or self.config.prometheus_url:
            try:
                from k8s_scale_test.health_sweep import AMPMetricCollector

                amp_collector = AMPMetricCollector(
                    amp_workspace_id=self.config.amp_workspace_id,
                    prometheus_url=self.config.prometheus_url,
                    aws_profile=getattr(self.config, "aws_profile", None),
                )
            except Exception as exc:
                log.warning("AMP collector init failed for anomaly detector: %s", exc)

        anomaly = AnomalyDetector(
            self.config, self.k8s_client, node_metrics, node_diag,
            self.evidence_store, run_id, self._prompt_operator,
            aws_client=self.aws_client,
            kb_store=kb_store,
            kb_matcher=kb_matcher,
            amp_collector=amp_collector,
            shared_ctx=self._shared_ctx,
        )
        sweep_agent = HealthSweepAgent(self.config, self.k8s_client, node_diag, self.evidence_store, run_id)
        infra_agent = InfraHealthAgent(self.k8s_client)
        monitor.on_alert(anomaly.handle_alert)
        watcher = EventWatcher(self.k8s_client, namespaces, self.evidence_store, config=self.config)

        # 5a. Set up agent context writer for AI sub-agent integration
        try:
            node_list: list[str] = []
            try:
                v1 = self.k8s_client.CoreV1Api()
                nodes = v1.list_node(watch=False, _request_timeout=10)
                node_list = [n.metadata.name for n in nodes.items]
            except Exception:
                log.debug("Could not fetch node list for agent context")
            self._ctx_writer = ContextFileWriter(self.evidence_store, run_id, self.config)
            self._ctx_writer.write_initial_context(namespaces, node_list)
        except Exception as exc:
            log.warning("Failed to initialize agent context writer: %s", exc)
            self._ctx_writer = None

        # ── Phase 6: Start Watchers ────────────────────────────────────
        await watcher.start(run_id, lambda: self._current_step)
        await monitor.start()

        # Launch an independent observer process that counts actual Running
        # pods via the pod list API — a completely different data path from
        # the monitor's deployment watch. Used for post-run cross-validation.
        observer_proc = await self._start_observer(run_id, namespaces)

        # ── Phase 6a: Observability Scanner ────────────────────────────
        # Create and start the proactive scanner if at least one backend
        # (AMP/Prometheus or CloudWatch) is configured.
        try:
            prom_fn = _make_prometheus_executor(self.config)
            cw_fn = _make_cloudwatch_executor(self.config, self.aws_client)
            if prom_fn or cw_fn:
                self._scanner = ObservabilityScanner(
                    self.config, prometheus_fn=prom_fn, cloudwatch_fn=cw_fn,
                )
                self._scanner.on_finding(self._on_scanner_finding)
                self._scanner_task = asyncio.create_task(self._scanner.run())
                log.info("ObservabilityScanner started (prom=%s, cw=%s)",
                         prom_fn is not None, cw_fn is not None)
            else:
                log.info("ObservabilityScanner skipped: no AMP/Prometheus or CloudWatch configured")
        except Exception as exc:
            log.warning("Failed to create ObservabilityScanner: %s", exc)
            self._scanner = None
            self._scanner_task = None

        # Create SharedContext when scanner exists, for cross-source correlation.
        if self._scanner:
            from k8s_scale_test.shared_context import SharedContext
            self._shared_ctx = SharedContext(
                max_age_seconds=self.config.event_time_window_minutes * 60,
            )
            anomaly.shared_ctx = self._shared_ctx

        # Create FindingReviewer for independent re-verification of findings.
        # Reuses the same amp_collector and cloudwatch_fn as the scanner.
        try:
            self._reviewer = FindingReviewer(
                evidence_store=self.evidence_store,
                run_id=run_id,
                amp_collector=amp_collector,
                cloudwatch_fn=cw_fn,
            )
        except Exception as exc:
            log.warning("Failed to create FindingReviewer: %s", exc)
            self._reviewer = None

        # ── Phase 7: Stressor Scaling + Hold at Peak ──────────────────
        # Scale pods, hold at target count, run health sweep. All protected
        # by try/finally so monitor/watcher/observer ALWAYS get stopped
        # even if scaling or the sweep throws an exception.
        try:
            if self._scanner:
                self._scanner.set_phase(ScanPhase.SCALING)
            with phase_span("scaling", run_id=run_id):
                result = await self._execute_scaling_via_flux(
                    writer, targets, deployments, anomaly,
                )

            # ── Phase 7a: Hold at Peak ─────────────────────────────────
            # Wait for the monitor to confirm target pod count, then hold
            # for the configured duration while running health checks.
            hold = getattr(self.config, 'hold_at_peak', 90)
            target = self.config.target_pods
            log.info("Waiting for monitor to confirm %d pods ready before hold...", target)
            for _ in range(30):
                ready, pending, _ = await self._count_pods()
                if ready >= target:
                    break
                await asyncio.sleep(5)

            # 7a. Hold at peak + node health sweep + Karpenter check
            # All run concurrently during the hold period
            if self._ctx_writer:
                self._ctx_writer.update_phase("hold-at-peak", datetime.now(timezone.utc))
            if self._scanner:
                self._scanner.set_phase(ScanPhase.HOLD_AT_PEAK)
            log.info("Holding at peak for %ds + running health checks...", hold)
            with phase_span("hold-at-peak", run_id=run_id, hold_seconds=hold):
                health_sweep_task = asyncio.create_task(sweep_agent.run(sample_size=10))
                hold_task = asyncio.create_task(asyncio.sleep(hold))

                await hold_task
                try:
                    self._health_sweep = await asyncio.wait_for(health_sweep_task, timeout=60)
                except asyncio.TimeoutError:
                    log.warning("Health sweep timed out after hold + 60s, proceeding with partial results")
                    self._health_sweep = {"nodes_sampled": 0, "healthy": 0,
                                          "issues": [], "timed_out": True}
                    health_sweep_task.cancel()

            # 7b. If sweep found issues, present to operator before cleanup
            sweep_issues = self._health_sweep.get("issues", [])
            if sweep_issues:
                sampled = self._health_sweep.get("nodes_sampled", 0)
                healthy = self._health_sweep.get("healthy", 0)
                log.warning("Health sweep: %d/%d nodes have issues at peak load",
                            sampled - healthy, sampled)
                await self._prompt_operator(
                    f"Node health sweep found {len(sweep_issues)} issue(s) at peak load. "
                    f"Review before cleanup?",
                    {"sampled": sampled, "healthy": healthy,
                     "issues": sweep_issues[:10]},
                )
        finally:
            # ALWAYS stop monitoring — even if scaling, hold, or sweep threw
            self._running = False  # Signal observer thread to exit
            await monitor.stop()
            await watcher.stop()
            self._stop_observer(observer_proc)
            # Stop the observability scanner
            if self._scanner:
                try:
                    await self._scanner.stop()
                    if self._scanner_task:
                        await self._scanner_task
                except Exception as exc:
                    log.warning("Scanner shutdown error: %s", exc)

        # Karpenter check runs after monitor stops — doesn't affect rate data
        self._karpenter_health = await infra_agent.check_if_needed(self._findings)

        # ── Phase 8: Cleanup ───────────────────────────────────────────
        # Reset all replicas to 0 and record the pod deletion rate.
        if self._ctx_writer:
            self._ctx_writer.update_phase("cleanup", datetime.now(timezone.utc))
        with phase_span("cleanup", run_id=run_id):
            await self._cleanup_pods(writer, all_labeled)

            # 8a. Collect CL2 result (if running concurrently)
            if cl2_task is not None:
                try:
                    cl2_summary = await cl2_task
                    if cl2_summary:
                        log.info("CL2 preload finished: %s", cl2_summary.test_status.status)
                except Exception as exc:
                    log.warning("CL2 preload failed: %s", exc)

            # 8b. CL2 cleanup (if preload was run)
            if self.config.cl2_preload:
                await self._cleanup_cl2()

        # ── Phase 9: Summary + Verification ───────────────────────────
        # Generate the test run summary, cross-validate rate data against
        # the observer, produce the HTML chart, and optionally auto-ingest
        # resolved findings into the Known Issues KB.

        # Await all pending finding review tasks before generating the summary.
        if self._review_tasks:
            await asyncio.gather(*self._review_tasks, return_exceptions=True)

        summary = self._make_summary(run_id, start, report, result)
        verification_issues = self._verify_run_data(run_id, result)

        # Downgrade validity if verification found timestamp gaps in the
        # monitoring data — this means the rate data is unreliable.
        if verification_issues and summary.validity == RunValidity.VALID:
            has_timestamp_gaps = any("timestamp gap" in i for i in verification_issues)
            if has_timestamp_gaps:
                summary.validity = RunValidity.INVALID
                summary.validity_reason = "monitoring_gaps"
                log.warning("Run downgraded to INVALID due to monitoring data gaps")

        self.evidence_store.save_summary(run_id, summary)

        # 8c. Auto-ingest resolved findings into KB
        if self.config.kb_auto_ingest and kb_store is not None:
            try:
                run_dir = str(self.evidence_store._run_dir(run_id))
                log.info("KB auto-ingest: processing findings from %s", run_dir)
                pipeline = IngestionPipeline(kb_store, kb_matcher or SignatureMatcher())
                ingested = pipeline.ingest_run(run_dir, self.evidence_store)
                if ingested:
                    log.info("KB auto-ingest: created/updated %d entries", len(ingested))
                else:
                    log.info("KB auto-ingest: no new entries from this run")
            except Exception as exc:
                log.warning("KB auto-ingest failed: %s", exc)

        try:
            from k8s_scale_test.chart import generate_chart
            run_dir = str(self.evidence_store._run_dir(run_id))
            step_dicts = [s.to_dict() for s in result.steps] if result.steps else []
            chart_path = generate_chart(run_dir, step_dicts)
            if chart_path:
                log.info("Chart: %s", chart_path)
                import webbrowser
                webbrowser.open(f"file://{os.path.abspath(chart_path)}")
        except Exception as exc:
            log.warning("Chart generation failed: %s", exc)

        log.info("Test run %s complete: %s", run_id, summary.validity.value)

        # 9. Drain nodes — non-critical, can take minutes
        await self._drain_nodes()

        return summary

    async def _run_preflight(self) -> PreflightReport:
        checker = PreflightChecker(self.config, self.k8s_client, self.aws_client)
        return await checker.run()

    async def _delete_events(self, namespaces: list[str]) -> None:
        """Delete all events in target namespaces for a clean baseline."""
        loop = asyncio.get_event_loop()
        try:
            v1 = self.k8s_client.CoreV1Api()
            for ns in namespaces:
                await loop.run_in_executor(None,
                    lambda n=ns: v1.delete_collection_namespaced_event(n))
                log.info("  Cleared events in %s", ns)
        except Exception as exc:
            log.warning("Event cleanup failed: %s", exc)

    async def _cleanup_pods(self, writer: FluxRepoWriter, deployments) -> None:
        """Reset replicas to 0 and record delete rate to rate_data.jsonl."""
        log.info("Cleanup: setting replicas to 0...")
        reset_targets = [(d.name, 0, d.source_path) for d in deployments]
        writer.set_replicas_batch(reset_targets)
        await self._git_commit_push("scale-test cleanup: reset all replicas to 0")

        start = datetime.now(timezone.utc)
        prev_ready = None
        prev_time = start
        while True:
            ready, _, total = await self._count_pods()
            now = datetime.now(timezone.utc)
            elapsed = (now - start).total_seconds()
            interval = (now - prev_time).total_seconds()

            if prev_ready is None:
                prev_ready = ready
                log.info("  Cleanup: %d pods to delete", ready)
            else:
                rate = (ready - prev_ready) / interval if interval > 0 else 0
                if ready < prev_ready:
                    delete_rate = (prev_ready - ready) / max(elapsed, 1)
                    remaining_min = (ready / delete_rate / 60) if delete_rate > 0 else 0
                    log.info("  Deleting: %d remaining (%.0f/s, ~%.1fm left)",
                             ready, delete_rate, remaining_min)

                if hasattr(self, '_evidence_run_id'):
                    from k8s_scale_test.models import RateDataPoint
                    dp = RateDataPoint(
                        timestamp=now, ready_count=ready,
                        delta_ready=ready - prev_ready, ready_rate=rate,
                        rolling_avg_rate=0.0, pending_count=0,
                        total_pods=total, interval_seconds=interval,
                        is_gap=False)
                    self.evidence_store.append_rate_datapoint(self._evidence_run_id, dp)

            if ready == 0 and total == 0:
                break
            if elapsed > 600:
                log.warning("  Cleanup timeout after 10m, %d pods remaining", ready)
                break
            prev_ready = ready
            prev_time = now
            await asyncio.sleep(10)

        log.info("  Pods deleted in %.1fm", (datetime.now(timezone.utc) - start).total_seconds() / 60)

    async def _drain_nodes(self) -> None:
        """Delete alt nodeclaims and wait for nodes to drain. Non-critical."""
        log.info("  Deleting alt nodeclaims...")
        node_start = datetime.now(timezone.utc)
        try:
            import subprocess
            subprocess.run(
                ["kubectl", "delete", "nodeclaims", "-l", "karpenter.sh/nodepool=alt", "--wait=false"],
                capture_output=True, timeout=30,
            )
        except Exception as exc:
            log.warning("  Nodeclaim delete failed: %s", exc)

        while True:
            nodes = await self._count_nodes()
            elapsed = (datetime.now(timezone.utc) - node_start).total_seconds()
            if nodes <= 3:
                break
            if elapsed > 600:
                log.warning("  Node drain timeout, %d nodes remaining", nodes)
                break
            log.info("  Nodes draining: %d remaining (%.0fs)", nodes, elapsed)
            await asyncio.sleep(15)

        log.info("  Nodes drained in %.1fm", (datetime.now(timezone.utc) - node_start).total_seconds() / 60)

    async def _git_commit_push(self, message: str) -> None:
        """Git add, commit, push, and trigger Flux reconciliation.

        All subprocess calls run in a thread pool executor to avoid
        blocking the asyncio event loop. The Flux reconcile command
        has a 60s timeout which would otherwise stall the entire
        monitoring pipeline.
        """
        import subprocess
        repo = self.config.flux_repo_path

        def _sync_git_and_flux():
            try:
                subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
                result = subprocess.run(
                    ["git", "diff", "--cached", "--quiet"], cwd=repo, capture_output=True,
                )
                if result.returncode == 0:
                    log.info("No changes to commit")
                    return
                subprocess.run(
                    ["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True,
                )
                subprocess.run(
                    ["git", "push"], cwd=repo, check=True, capture_output=True,
                )
                log.info("Committed and pushed: %s", message)
                # Trigger Flux reconciliation so changes apply immediately
                try:
                    subprocess.run(
                        ["flux", "reconcile", "kustomization", "app", "--with-source"],
                        check=True, capture_output=True, timeout=60,
                    )
                    log.info("Flux reconciliation triggered")
                except Exception as exc:
                    log.info("Flux reconcile timed out (will auto-sync): %s", exc)
            except subprocess.CalledProcessError as exc:
                log.error("Git operation failed: %s\nstderr: %s", exc, exc.stderr.decode() if exc.stderr else "")

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _sync_git_and_flux)

    async def _suspend_flux(self) -> None:
        """No longer needed — scaling is done through Flux repo directly."""
        pass

    async def _execute_scaling_via_flux(
        self,
        writer: FluxRepoWriter,
        targets: list[tuple[str, int, str]],
        deployments,
        anomaly: AnomalyDetector,
    ) -> ScalingResult:
        """Scale pods by committing target replicas to the Flux Git repo.

        Strategy: one git commit sets the full target replica count across
        all deployments → one Flux reconcile picks it up → K8s handles the
        rest. There are no incremental steps — the entire target is set at
        once and we monitor until all pods are Ready or we hit the timeout.

        The polling loop (every 5s) does four things:
        1. Reads pod counts from the monitor (no API call — the monitor's
           watch threads maintain real-time counts).
        2. Refreshes the K8s token every 10 minutes (EKS tokens expire
           after 15 minutes; refreshing at 10 prevents mid-scale auth failures).
        3. Triggers background diagnostics once after 60s if pods are still
           Pending (gives the anomaly detector early signal without blocking).
        4. Breaks when ready >= target or elapsed > timeout.

        Parameters
        ----------
        writer : FluxRepoWriter
            Writes replica counts to Flux-managed YAML manifests.
        targets : list[tuple[str, int, str]]
            ``(deployment_name, replica_count, source_path)`` tuples.
        deployments :
            All discovered deployments (for namespace extraction).
        anomaly : AnomalyDetector
            Handles timeout alerts if scaling doesn't complete.

        Returns
        -------
        ScalingResult
            Summary of the scaling operation.
        """
        if not targets:
            log.warning("No deployments found to scale")
            return ScalingResult(
                steps=[], total_pods_requested=0, total_pods_ready=0,
                total_nodes_provisioned=0, peak_ready_rate=0.0,
                completed=False, halt_reason="no_deployments",
            )

        target = self.config.target_pods
        timeout = self.config.pending_timeout_seconds
        self._current_step = 1

        # Update agent context to scaling phase
        if self._ctx_writer:
            self._ctx_writer.update_phase("scaling", datetime.now(timezone.utc))

        # Set full target in one commit
        results = writer.set_replicas_batch(targets)
        failed = [n for n, ok in results.items() if not ok]
        if failed:
            log.warning("Failed to update manifests for: %s", failed)

        await self._git_commit_push(f"scale-test: {target} pods")

        # Monitor until target reached or timeout
        start = datetime.now(timezone.utc)
        last_log_time = start
        last_token_refresh = start
        diag_done = False
        peak_rate = 0.0

        while True:
            await asyncio.sleep(5.0)
            ready, pending, total = await self._count_pods()
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            elapsed_min = elapsed / 60

            # Feed live data to the observability scanner
            if self._scanner:
                self._scanner.update_context(
                    elapsed_minutes=elapsed_min, pending=pending, ready=ready,
                )

            # Proactively refresh K8s token every 10 minutes during long scaling
            now = datetime.now(timezone.utc)
            if (now - last_token_refresh).total_seconds() >= 600:
                self._refresh_k8s_token()
                last_token_refresh = now

            # Track peak rate from the monitor
            if self._monitor:
                current_rate = self._monitor.get_current_rate()
                if current_rate > peak_rate:
                    peak_rate = current_rate

            # Log progress every 30s
            now = datetime.now(timezone.utc)
            if (now - last_log_time).total_seconds() >= 30:
                log.info("  Scaling: ready=%d pending=%d total=%d (%.1fm)",
                         ready, pending, total, elapsed_min)
                last_log_time = now

            # Run diagnostics once after 60s if pods still pending — background task
            if not diag_done and elapsed > 60 and pending > 0:
                diag_done = True
                log.info("Running diagnostics — %d pods pending after %.0fs", pending, elapsed)
                # Run in a separate thread with its own event loop so it never
                # blocks the controller's polling loop or the monitor's ticker.
                def _diag_thread():
                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(self._run_diagnostics(anomaly, deployments, pending, ready))
                    except Exception as e:
                        log.error("Diagnostics thread failed: %s", e)
                    finally:
                        loop.close()
                threading.Thread(target=_diag_thread, daemon=True, name="diagnostics").start()

            if pending == 0 and ready >= target:
                break
            if elapsed > timeout:
                log.warning("Timeout: ready=%d pending=%d after %.1fm", ready, pending, elapsed_min)
                alert = Alert(
                    alert_type=AlertType.PENDING_TIMEOUT,
                    timestamp=now,
                    message=f"Timeout: {pending} pods pending after {elapsed:.0f}s",
                    context={"pending": pending, "ready": ready, "step": 1},
                )
                if self._ctx_writer:
                    self._ctx_writer.append_alert(alert)
                finding = await anomaly.handle_alert(alert)
                self._findings.append(finding)
                if self._ctx_writer:
                    self._ctx_writer.append_finding_summary(finding)
                if self._reviewer is not None:
                    task = asyncio.create_task(self._safe_review(finding))
                    self._review_tasks.append(task)
                if finding.severity == Severity.CRITICAL:
                    await self._prompt_operator(
                        f"Critical finding at timeout", {"finding": finding.finding_id})
                break

        duration = (datetime.now(timezone.utc) - start).total_seconds()
        ready, pending, total = await self._count_pods()
        nodes = await self._count_nodes()

        steps = [ScalingStep(
            step_number=1, timestamp=start,
            target_replicas=target, actual_ready=ready,
            actual_pending=pending, duration_seconds=duration,
        )]

        log.info("Scaling complete: ready=%d/%d in %.1fm", ready, target, duration / 60)

        return ScalingResult(
            steps=steps, total_pods_requested=target,
            total_pods_ready=ready, total_nodes_provisioned=nodes,
            peak_ready_rate=peak_rate, completed=ready >= target,
            halt_reason=None if ready >= target else "target_not_reached",
        )

    async def _start_observer(self, run_id: str, namespaces: list[str]):
        """Launch an independent observer that counts actual Running pods.

        The monitor uses the K8s Deployment watch API (.status.readyReplicas).
        The observer uses a completely different data path — it lists actual
        pods via the pod API and counts those in Running phase. This catches
        discrepancies between what the deployment controller reports and what's
        actually running on the cluster.

        Polls every 10s (offset from the monitor's 5s tick) to avoid
        synchronized API pressure.

        Writes to {run_dir}/observer.log for post-run cross-validation.
        """
        rd = self.evidence_store._run_dir(run_id)
        obs_file = rd / "observer.log"
        ns_list = namespaces if namespaces else ["default"]

        obs_file.write_text("timestamp,namespace running pending total\n")

        try:
            import threading

            def _poller():
                consecutive_errors = 0
                v1 = self.k8s_client.CoreV1Api()
                try:
                    with open(obs_file, "a") as fh:
                        while self._running:
                            try:
                                total_running = 0
                                total_pending = 0
                                total_all = 0
                                for ns in ns_list:
                                    # Paginate with limit=5000 to avoid 80-130MB
                                    # single responses that cause IncompleteRead.
                                    # Use (connect, read) timeout tuple — 60s read
                                    # is needed for large paginated responses.
                                    _continue = None
                                    while True:
                                        kwargs = dict(
                                            watch=False,
                                            _request_timeout=(5, 60),
                                            field_selector="status.phase!=Succeeded,status.phase!=Failed",
                                            limit=5000,
                                        )
                                        if _continue:
                                            kwargs["_continue"] = _continue
                                        pods = v1.list_namespaced_pod(ns, **kwargs)
                                        for pod in pods.items:
                                            phase = pod.status.phase if pod.status else ""
                                            total_all += 1
                                            if phase == "Running":
                                                total_running += 1
                                            elif phase == "Pending":
                                                total_pending += 1
                                        _continue = pods.metadata._continue
                                        if not _continue:
                                            break
                                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                                fh.write(f"{ts},all {total_running} {total_pending} {total_all}\n")
                                fh.flush()
                                consecutive_errors = 0
                            except Exception as e:
                                consecutive_errors += 1
                                err_str = str(e)
                                # Refresh K8s token on auth errors
                                if any(x in err_str for x in ("Unauthorized", "401", "ExpiredToken")):
                                    if hasattr(self.config, '_k8s_reload'):
                                        try:
                                            self.config._k8s_reload()
                                            v1 = self.k8s_client.CoreV1Api()
                                        except Exception:
                                            pass
                                if consecutive_errors <= 3 or consecutive_errors % 10 == 0:
                                    log.debug("Observer poll error (%d): %s",
                                              consecutive_errors, type(e).__name__)
                            _time.sleep(10)
                except Exception as e:
                    log.warning("Observer thread exited: %s", e)

            t = threading.Thread(target=_poller, daemon=True)
            t.start()
            log.info("Observer started: pod-list poll every 10s across %d namespace(s) → %s",
                     len(ns_list), obs_file)
            return _ObserverHandle(t)
        except Exception as exc:
            log.warning("Observer failed to start: %s", exc)
            return None

    @staticmethod
    def _stop_observer(proc):
        """Terminate the observer process or thread."""
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
            log.info("Observer stopped")
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    async def _run_diagnostics(self, anomaly, deployments, pending, ready) -> None:
        """Run diagnostics in background — never blocks the scaling loop."""
        try:
            namespaces = list({d.namespace for d in deployments})
            alert = Alert(
                alert_type=AlertType.PENDING_TIMEOUT,
                timestamp=datetime.now(timezone.utc),
                message=f"Diagnostics: {pending} pods pending",
                context={"pending": pending, "ready": ready, "step": 1, "namespaces": namespaces},
            )
            if self._ctx_writer:
                self._ctx_writer.append_alert(alert)
            finding = await anomaly.handle_alert(alert)
            self._findings.append(finding)
            if self._ctx_writer:
                self._ctx_writer.append_finding_summary(finding)
            if self._reviewer is not None:
                await self._safe_review(finding)
            if finding.k8s_events:
                warnings = [e for e in finding.k8s_events if e.event_type == "Warning"]
                if warnings:
                    reasons = {}
                    for e in warnings:
                        reasons[e.reason] = reasons.get(e.reason, 0) + 1
                    log.warning("  Warning events: %s",
                                ", ".join(f"{r}={c}" for r, c in sorted(reasons.items(), key=lambda x: -x[1])[:5]))
        except Exception as exc:
            log.error("Diagnostics failed: %s", exc)




    async def _prompt_operator(self, message: str, context: dict) -> str:
        """Present a message to the operator and wait for input."""
        print(f"\n{'='*60}")
        print(f"[OPERATOR] {message}")
        for k, v in context.items():
            print(f"  {k}: {v}")
        print(f"{'='*60}")
        if self.config.auto_approve:
            log.info("Auto-approving: %s", message)
            return "continue"
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: input("Response> "))
            log.info("Operator response: %s", response)
            return response
        except (EOFError, KeyboardInterrupt):
            return "continue"

    def _on_scanner_finding(self, result: ScanResult) -> None:
        """Handle a scanner finding: log, persist, collect, share."""
        log.warning("Scanner [%s] %s: %s", result.query_name, result.severity.value, result.title)
        try:
            self.evidence_store.save_scanner_finding(self._evidence_run_id, result)
        except Exception as exc:
            log.debug("Failed to persist scanner finding: %s", exc)
        self._scanner_findings.append(result)
        # Write to shared context for cross-source correlation
        if self._shared_ctx is not None:
            try:
                self._shared_ctx.add(result)
            except Exception as exc:
                log.debug("Failed to write scanner finding to shared context: %s", exc)

    async def _safe_review(self, finding: Finding) -> None:
        """Run a finding review, catching all exceptions."""
        try:
            await self._reviewer.review(finding)
        except Exception as exc:
            log.warning("Finding review failed for %s: %s", finding.finding_id, exc)

    async def _count_pods(self) -> tuple[int, int, int]:
        """Return ``(ready, pending, total)`` pod counts.

        Reads from the monitor's watch-maintained counters (instant, no API
        call). Falls back to a direct K8s API call only if the monitor isn't
        running yet (e.g., during preflight or cleanup).
        """
        if hasattr(self, '_monitor') and self._monitor._running:
            return self._monitor.get_counts()
        # Fallback: direct API call only if monitor isn't running
        loop = asyncio.get_event_loop()
        for attempt in range(2):
            try:
                def _sync_count():
                    apps_v1 = self.k8s_client.AppsV1Api()
                    ready = pending = total = 0
                    for ns in self._namespaces:
                        deps = apps_v1.list_namespaced_deployment(ns, watch=False,
                                                                  _request_timeout=10)
                        for d in deps.items:
                            r = d.status.replicas or 0
                            rd = d.status.ready_replicas or 0
                            total += r
                            ready += rd
                            pending += r - rd
                    return ready, pending, total
                return await loop.run_in_executor(None, _sync_count)
            except Exception as exc:
                if attempt == 0 and ("Unauthorized" in str(exc) or "401" in str(exc)):
                    log.warning("K8s token expired, refreshing...")
                    self._refresh_k8s_token()
                    continue
                log.error("Failed to count pods: %s", exc)
                return 0, 0, 0
        return 0, 0, 0

    async def _count_nodes(self) -> int:
        """Return the current node count.

        Reads from the monitor's watch-maintained counter. Falls back to
        a direct K8s API call if the monitor isn't running.
        """
        if hasattr(self, '_monitor') and self._monitor._running:
            return self._monitor.get_node_count()
        # Fallback: direct API call
        for attempt in range(2):
            try:
                v1 = self.k8s_client.CoreV1Api()
                nodes = v1.list_node(watch=False)
                return len(nodes.items)
            except Exception as exc:
                if attempt == 0 and ("Unauthorized" in str(exc) or "401" in str(exc)):
                    log.warning("K8s token expired, refreshing...")
                    self._refresh_k8s_token()
                    continue
                log.error("Failed to count nodes: %s", exc)
                return 0
        return 0

    def _refresh_k8s_token(self) -> None:
        """Reload kubeconfig to get a fresh EKS token."""
        if hasattr(self.config, '_k8s_reload'):
            try:
                self.config._k8s_reload()
                log.info("K8s token refreshed")
            except Exception as exc:
                log.error("Failed to refresh K8s token: %s", exc)

    def _verify_run_data(self, run_id: str, result) -> list[str]:
        """Cross-validate rate_data.jsonl (from deployment watch) against observer.log (from pod list API).

        The monitor and observer use completely different K8s API paths:
        - Monitor: watches Deployment .status.readyReplicas (fast, event-driven)
        - Observer: lists actual pods in Running phase (slower, poll-based)

        Discrepancies between them indicate deployment controller lag,
        monitor watch disconnects, or API server inconsistencies.

        Checks performed:
        1. Data point count vs expected (duration / 5s tick interval)
        2. Peak ready count matches between monitor and controller
        3. Gaps in monitoring data (is_gap flag + timestamp-based detection)
        4. Observer vs monitor peak count and peak rate cross-check

        Returns a list of issue strings (empty if all checks pass).
        """
        import json
        from pathlib import Path

        rd = self.evidence_store._run_dir(run_id)
        rate_file = rd / "rate_data.jsonl"
        issues = []

        if not rate_file.exists():
            log.warning("Verification: no rate_data.jsonl found")
            return ["no rate_data.jsonl found"]

        points = [json.loads(l) for l in rate_file.read_text().splitlines() if l.strip()]
        if not points:
            log.warning("Verification: rate_data.jsonl is empty")
            return ["rate_data.jsonl is empty"]

        # Check 1: data point count vs expected (duration / 5s)
        if result.steps:
            expected_pts = int(result.steps[0].duration_seconds / 5) - 2
            if len(points) < expected_pts:
                issues.append(f"Only {len(points)} data points, expected ~{expected_pts}+ for {result.steps[0].duration_seconds:.0f}s test")

        # Check 2: peak ready in rate_data matches controller result
        rate_peak = max(p.get("ready_count", 0) for p in points)
        if result.total_pods_ready > 0 and abs(rate_peak - result.total_pods_ready) > 100:
            issues.append(f"Peak ready mismatch: rate_data={rate_peak} controller={result.total_pods_ready}")

        # Check 3: gaps in monitoring
        gaps = [p for p in points if p.get("is_gap")]
        if gaps:
            issues.append(f"{len(gaps)} gap(s) in rate data — chart rates may be averaged over long intervals")

        # Check 3a: timestamp-based gap detection — catches gaps during
        # active scaling where the rate data should be continuous.
        # "Active scaling" = pending > 0 (pods are being created/scheduled).
        # Gaps during infrastructure wait, hold-at-peak, and cleanup are
        # expected and not flagged.
        from datetime import datetime as _dt_gap
        active_scaling_points = [
            p for p in points if p.get("pending_count", 0) > 0
        ]

        gap_threshold = self._TICK_INTERVAL * 3 if hasattr(self, '_TICK_INTERVAL') else 15.0
        timestamp_gaps = []
        for i in range(1, len(active_scaling_points)):
            try:
                t0 = _dt_gap.fromisoformat(active_scaling_points[i-1]["timestamp"].replace("Z", "+00:00"))
                t1 = _dt_gap.fromisoformat(active_scaling_points[i]["timestamp"].replace("Z", "+00:00"))
                interval = (t1 - t0).total_seconds()
                if interval > gap_threshold:
                    timestamp_gaps.append((i, interval))
            except Exception:
                continue
        if timestamp_gaps:
            gap_details = ", ".join(f"point {i}: {iv:.0f}s" for i, iv in timestamp_gaps[:5])
            issues.append(f"{len(timestamp_gaps)} timestamp gap(s) >15s in active scaling data: {gap_details}")

        # Check 4: observer cross-check
        # Observer uses pod list API (actual Running pods) vs monitor's deployment watch
        # (.status.readyReplicas). Discrepancies indicate deployment controller lag or
        # monitor watch disconnects.
        obs_file = rd / "observer.log"
        if not obs_file.exists():
            obs_file = Path("observer.log")
        if obs_file.exists():
            obs_peak = 0
            obs_lines = [l.strip() for l in obs_file.read_text().splitlines()
                         if l.strip() and not l.startswith("timestamp")]
            for line in obs_lines:
                parts = line.split(",", 1)
                if len(parts) < 2:
                    continue
                fields = parts[1].split()
                try:
                    val = int(fields[1])
                    obs_peak = max(obs_peak, val)
                except (ValueError, IndexError):
                    continue

            if obs_peak > 0:
                # Cross-check peak pod count
                if abs(obs_peak - rate_peak) > 500:
                    issues.append(f"Observer vs monitor peak mismatch: observer={obs_peak} monitor={rate_peak}")
                else:
                    log.info("Verification: observer peak cross-check passed (observer=%d, monitor=%d)",
                             obs_peak, rate_peak)

                # Cross-check rate: compute observer's scaling rate
                obs_data = []
                from datetime import datetime as _dt
                for line in obs_lines:
                    parts = line.split(",", 1)
                    if len(parts) < 2:
                        continue
                    ts_str = parts[0]
                    fields = parts[1].split()
                    try:
                        ready = int(fields[1])
                        obs_data.append((ts_str, ready))
                    except (ValueError, IndexError):
                        continue

                if len(obs_data) > 2:
                    obs_rates = []
                    for i in range(1, len(obs_data)):
                        try:
                            t0 = _dt.fromisoformat(obs_data[i-1][0].replace("Z", "+00:00"))
                            t1 = _dt.fromisoformat(obs_data[i][0].replace("Z", "+00:00"))
                            dt = (t1 - t0).total_seconds()
                            if dt > 0:
                                r = (obs_data[i][1] - obs_data[i-1][1]) / dt
                                if r > 0:
                                    obs_rates.append(r)
                        except Exception:
                            continue

                    if obs_rates:
                        obs_peak_rate = max(obs_rates)
                        monitor_rates = [p["ready_rate"] for p in points
                                         if p["ready_rate"] > 0 and not p.get("is_gap")]
                        monitor_peak_rate = max(monitor_rates) if monitor_rates else 0

                        # Allow 10x tolerance — the monitor sees 5s burst spikes
                        # while the observer averages over 10s+ poll intervals.
                        # At 30K pods the observer may also lose polls to
                        # IncompleteRead, further smoothing its peak rate.
                        if monitor_peak_rate > 0 and obs_peak_rate > 0:
                            ratio = monitor_peak_rate / obs_peak_rate
                            if ratio > 10.0 or ratio < 0.1:
                                issues.append(
                                    f"Rate mismatch: monitor_peak={monitor_peak_rate:.1f}/s "
                                    f"observer_peak={obs_peak_rate:.1f}/s (ratio={ratio:.1f}x)")
                            else:
                                log.info("Verification: rate cross-check passed "
                                         "(monitor=%.1f/s, observer=%.1f/s, ratio=%.1fx)",
                                         monitor_peak_rate, obs_peak_rate, ratio)
            else:
                issues.append("Observer log exists but no valid data — pod list polling may have failed")
        else:
            issues.append("No observer log found — independent rate validation unavailable")

        if issues:
            log.warning("Verification issues:")
            for i in issues:
                log.warning("  - %s", i)
        else:
            log.info("Verification: all checks passed (%d points, peak=%d)", len(points), rate_peak)
        return issues

    @staticmethod
    def _classify_run_validity(
        result: ScalingResult, findings: list[Finding], target: int,
    ) -> tuple[RunValidity, str]:
        """Classify whether the test run produced valid data.

        The primary criterion is simple: did we reach the target pod count?
        Findings (anomaly investigations) are informational — they don't
        invalidate a run that successfully scaled to target.

        Returns
        -------
        tuple[RunValidity, str]
            ``(VALID|INVALID, reason_string)``
        """
        if not result.completed:
            return RunValidity.INVALID, result.halt_reason or "target_not_reached"
        if result.total_pods_ready < target:
            return RunValidity.INVALID, "not_all_ready"
        return RunValidity.VALID, "success"

    def _make_summary(
        self, run_id: str, start: datetime,
        report: PreflightReport, result: ScalingResult,
    ) -> TestRunSummary:
        end = datetime.now(timezone.utc)
        validity, reason = self._classify_run_validity(
            result, self._findings, self.config.target_pods,
        )
        agent_findings = self.evidence_store.load_agent_findings(run_id) or None
        scanner_findings = [
            {
                "query_name": r.query_name,
                "severity": r.severity.value,
                "title": r.title,
                "detail": r.detail,
                "source": r.source.value,
            }
            for r in self._scanner_findings
        ] or None
        return TestRunSummary(
            run_id=run_id, start_time=start, end_time=end,
            duration_seconds=(end - start).total_seconds(),
            config=self.config, preflight=report,
            scaling_result=result,
            peak_pod_count=result.total_pods_ready,
            peak_ready_rate=result.peak_ready_rate,
            total_nodes_provisioned=result.total_nodes_provisioned,
            anomaly_count=len(self._findings),
            findings=self._findings,
            validity=validity, validity_reason=reason,
            node_health_sweep=self._health_sweep or None,
            karpenter_health=self._karpenter_health or None,
            agent_findings=agent_findings,
            scanner_findings=scanner_findings,
        )

    # ------------------------------------------------------------------
    # CL2 Preload Methods (local subprocess approach)
    # ------------------------------------------------------------------

    def _read_cl2_config_template(self, name: str) -> str:
        """Read a CL2 config template from configs/clusterloader2/.

        Looks in the project root first (next to pyproject.toml),
        then falls back to flux_repo_path/apps/base/clusterloader2/configs/.
        Raises FileNotFoundError if the template doesn't exist.
        """
        from pathlib import Path

        # Try project root first
        project_root = Path(__file__).resolve().parent.parent.parent
        config_path = project_root / "configs" / "clusterloader2" / f"{name}.yaml"
        if config_path.exists():
            return config_path.read_text()

        # Fallback to flux repo path
        config_path = (
            Path(self.config.flux_repo_path)
            / "apps" / "base" / "clusterloader2" / "configs"
            / f"{name}.yaml"
        )
        if config_path.exists():
            return config_path.read_text()

        raise FileNotFoundError(
            f"CL2 config template not found: {name}.yaml"
        )

    def _find_cl2_binary(self) -> str:
        """Locate the clusterloader2 binary. Checks project bin/, workspace bin/, then PATH."""
        from pathlib import Path
        import shutil

        # Check project root bin/ first
        project_bin = Path(__file__).resolve().parent.parent.parent / "bin" / "clusterloader2"
        if project_bin.exists():
            return str(project_bin)

        # Check workspace bin/ (flux repo parent)
        workspace_bin = Path(self.config.flux_repo_path).parent / "bin" / "clusterloader2"
        if workspace_bin.exists():
            return str(workspace_bin)

        # Check PATH
        found = shutil.which("clusterloader2")
        if found:
            return found

        raise FileNotFoundError(
            "clusterloader2 binary not found. Build it with: "
            "git clone --depth 1 https://github.com/kubernetes/perf-tests.git /tmp/perf-tests && "
            "cd /tmp/perf-tests/clusterloader2 && go build -o ./bin/clusterloader2 ./cmd"
        )

    async def _run_cl2_preload(self) -> CL2Summary | None:
        """Execute CL2 preload phase as a local subprocess.

        Auto-computes object counts proportional to --target-pods unless
        explicit --cl2-params are provided. Logs the full preload plan
        and stores it in the CL2Summary for the evidence store.

        Returns CL2Summary on success, or None if the operator aborts.
        """
        import subprocess
        import tempfile
        import json
        from pathlib import Path
        from k8s_scale_test.models import CL2PreloadPlan

        config_name = self.config.cl2_preload
        timeout = self.config.cl2_timeout
        cl2_start = datetime.now(timezone.utc)

        log.info("CL2 preload: starting with config '%s' (local subprocess)", config_name)

        # 1. Find CL2 binary
        try:
            cl2_bin = self._find_cl2_binary()
        except FileNotFoundError as exc:
            log.error("CL2 preload aborted: %s", exc)
            raise

        # 2. Compute preload plan — auto-scale from target_pods or use explicit params
        if self.config.cl2_params:
            # User provided explicit params — build plan from them
            p = self.config.cl2_params
            plan = CL2PreloadPlan(
                target_pods=self.config.target_pods,
                namespaces=int(p.get("NAMESPACES", "10")),
                deployments_per_ns=int(p.get("DEPLOYMENTS_PER_NS", "5")),
                pod_replicas=int(p.get("POD_REPLICAS", "2")),
                services_per_ns=int(p.get("SERVICES_PER_NS", "5")),
                configmaps_per_ns=int(p.get("CONFIGMAPS_PER_NS", "10")),
                secrets_per_ns=int(p.get("SECRETS_PER_NS", "5")),
            )
        else:
            # Auto-compute proportional to target pod count
            plan = CL2PreloadPlan.from_target_pods(self.config.target_pods)

        # Log the plan clearly
        log.info("CL2 preload plan (proportional to %d target pods):", plan.target_pods)
        log.info("  Namespaces:    %d", plan.namespaces)
        log.info("  Deployments:   %d (%d/ns × %d ns)", plan.total_deployments, plan.deployments_per_ns, plan.namespaces)
        log.info("  Pods:          %d (%d replicas × %d deployments)", plan.total_pods, plan.pod_replicas, plan.total_deployments)
        log.info("  Services:      %d (%d/ns)", plan.total_services, plan.services_per_ns)
        log.info("  ConfigMaps:    %d (%d/ns × 3 sizes)", plan.total_configmaps, plan.configmaps_per_ns)
        log.info("  Secrets:       %d (%d/ns)", plan.total_secrets, plan.secrets_per_ns)
        log.info("  Total objects: %d", plan.total_objects)

        # 3. Read config template
        try:
            config_content = self._read_cl2_config_template(config_name)
        except FileNotFoundError as exc:
            log.error("CL2 preload aborted: %s", exc)
            raise

        # 4. Write config to temp file
        report_dir = tempfile.mkdtemp(prefix="cl2-results-")
        config_file = os.path.join(report_dir, "config.yaml")
        with open(config_file, "w") as fh:
            fh.write(config_content)

        # 5. Resolve kubeconfig
        kubeconfig = self.config.kubeconfig or os.path.expanduser("~/.kube/config")

        # 6. Copy object templates alongside the config file so CL2 can find them
        from pathlib import Path as _Path
        project_root = _Path(__file__).resolve().parent.parent.parent
        configs_dir = project_root / "configs" / "clusterloader2"
        if not configs_dir.exists():
            configs_dir = (
                _Path(self.config.flux_repo_path)
                / "apps" / "base" / "clusterloader2" / "configs"
            )
        for tmpl in configs_dir.glob("*.yaml"):
            if tmpl.name != f"{config_name}.yaml":
                import shutil
                shutil.copy2(tmpl, os.path.join(report_dir, tmpl.name))

        # 7. Build CL2 command
        cmd = [
            cl2_bin,
            f"--testconfig={config_file}",
            f"--kubeconfig={kubeconfig}",
            f"--report-dir={report_dir}",
            "--provider=eks",
            "--enable-exec-service=false",
            "--delete-automanaged-namespaces=false",  # Keep objects alive until our cleanup
            "--v=2",
        ]

        # Write overrides YAML with the computed plan values
        overrides_file = os.path.join(report_dir, "overrides.yaml")
        with open(overrides_file, "w") as fh:
            fh.write(f"NAMESPACES: \"{plan.namespaces}\"\n")
            fh.write(f"DEPLOYMENTS_PER_NS: \"{plan.deployments_per_ns}\"\n")
            fh.write(f"POD_REPLICAS: \"{plan.pod_replicas}\"\n")
            fh.write(f"SERVICES_PER_NS: \"{plan.services_per_ns}\"\n")
            fh.write(f"CONFIGMAPS_PER_NS: \"{plan.configmaps_per_ns}\"\n")
            fh.write(f"SECRETS_PER_NS: \"{plan.secrets_per_ns}\"\n")
        cmd.append(f"--testoverrides={overrides_file}")

        log.info("CL2 command: %s", " ".join(cmd))

        # 8. Start peak object tracker concurrently
        cl2_stop = asyncio.Event()
        tracker_task = asyncio.create_task(self._track_cl2_peak_objects(plan, cl2_stop))

        # 9. Run CL2 as subprocess
        loop = asyncio.get_event_loop()
        try:
            def _run_cl2():
                return subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=int(timeout),
                )

            result = await loop.run_in_executor(None, _run_cl2)
            duration = (datetime.now(timezone.utc) - cl2_start).total_seconds()

            # Log CL2 output
            if result.stdout:
                for line in result.stdout.strip().split("\n")[-20:]:
                    log.info("  CL2: %s", line)
            if result.stderr:
                # Surface fatal/panic lines as errors
                for line in result.stderr.strip().split("\n"):
                    if any(k in line for k in ["panic:", "FATAL", "F0", "fatal"]):
                        log.error("  CL2 FATAL: %s", line.strip())
                # Only log stderr details at debug level — it's mostly verbose CL2 internals
                for line in result.stderr.strip().split("\n")[-10:]:
                    log.debug("  CL2 stderr: %s", line)

            if result.returncode == 0:
                status = "Complete"
            else:
                status = "Failed"
                log.warning("CL2 exited with code %d", result.returncode)

        except subprocess.TimeoutExpired:
            duration = (datetime.now(timezone.utc) - cl2_start).total_seconds()
            status = "Timeout"
            log.warning("CL2 timed out after %.0fs", duration)
        except Exception as exc:
            duration = (datetime.now(timezone.utc) - cl2_start).total_seconds()
            status = "Failed"
            log.error("CL2 subprocess failed: %s", exc)

        # 10. Stop peak tracker and collect peak counts
        cl2_stop.set()
        await tracker_task

        # 11. Parse results from report-dir
        run_name = f"cl2-{config_name}"
        if status == "Complete":
            try:
                summary = CL2ResultParser.parse_report_dir(report_dir)
                summary.test_status = CL2TestStatus(
                    config_name=config_name,
                    job_name=run_name,
                    status="Passed",
                    duration_seconds=duration,
                )
                summary.preload_plan = plan
            except CL2ParseError as exc:
                log.debug("CL2 result parsing skipped (no PerfData): %s", exc)
                summary = CL2Summary(
                    test_status=CL2TestStatus(
                        config_name=config_name,
                        job_name=run_name,
                        status="Failed",
                        duration_seconds=duration,
                        error_message=f"Parse error: {exc}",
                    ),
                    preload_plan=plan,
                    pod_startup_latencies=[],
                    api_latencies=[],
                    api_availability=None,
                    scheduling_throughput=None,
                    raw_results={},
                )

            self.evidence_store.save_cl2_summary(self._evidence_run_id, summary)
            self._cl2_summary = summary
            log.info("CL2 preload complete: %s (%.0fs, %d objects created)", summary.test_status.status, duration, plan.total_objects)
            return summary

        error_msg = f"CL2 {status.lower()} after {duration:.0f}s"
        summary = CL2Summary(
            test_status=CL2TestStatus(
                config_name=config_name,
                job_name=run_name,
                status=status,
                duration_seconds=duration,
                error_message=error_msg,
            ),
            preload_plan=plan,
            pod_startup_latencies=[],
            api_latencies=[],
            api_availability=None,
            scheduling_throughput=None,
            raw_results={},
        )
        self.evidence_store.save_cl2_summary(self._evidence_run_id, summary)
        self._cl2_summary = summary

        response = await self._prompt_operator(
            f"CL2 preload {status.lower()}: {error_msg}. Continue with pod scaling or abort?",
            {"config": config_name, "status": status, "duration": f"{duration:.0f}s",
             "planned_objects": plan.total_objects},
        )
        if response and response.lower() in ("abort", "no", "cancel"):
            log.info("Operator chose to abort after CL2 %s", status.lower())
            return None

        log.info("Operator chose to continue after CL2 %s", status.lower())
        return summary

    async def _track_cl2_peak_objects(self, plan, stop_event: asyncio.Event) -> None:
        """Watch CL2 object creation via K8s watch API and record peak counts per type.

        Uses the informer pattern: LIST first to get initial state + resource_version,
        then WATCH from that resource_version to stream changes.
        """
        import threading

        peak = {"ns": 0, "dep": 0, "pod": 0, "svc": 0, "cm": 0, "sec": 0}
        current = {"ns": 0, "dep": 0, "svc": 0, "cm": 0, "sec": 0}
        lock = threading.Lock()

        def _watch_resource(list_func, resource_key):
            """Informer pattern: chunked LIST then WATCH from resource_version."""
            from kubernetes import watch as kw

            while not stop_event.is_set():
                try:
                    # Chunked LIST to get initial state without huge responses
                    initial_count = 0
                    rv = None
                    _continue = None
                    while True:
                        kwargs = {"watch": False, "limit": 500}
                        if _continue:
                            kwargs["_continue"] = _continue
                        resp = list_func(**kwargs)
                        for item in resp.items:
                            ns = item.metadata.namespace or item.metadata.name
                            if ns.startswith("cl2-test-"):
                                initial_count += 1
                        rv = resp.metadata.resource_version
                        _continue = resp.metadata._continue if resp.metadata else None
                        if not _continue:
                            break

                    with lock:
                        current[resource_key] = initial_count
                        peak[resource_key] = max(peak[resource_key], initial_count)

                    # WATCH from that resource_version
                    w = kw.Watch()
                    for event in w.stream(list_func, resource_version=rv, timeout_seconds=600):
                        if stop_event.is_set():
                            w.stop()
                            return
                        obj = event["object"]
                        ns = obj.metadata.namespace or obj.metadata.name
                        if not ns.startswith("cl2-test-"):
                            continue
                        etype = event["type"]
                        with lock:
                            if etype == "ADDED":
                                current[resource_key] += 1
                            elif etype == "DELETED":
                                current[resource_key] = max(0, current[resource_key] - 1)
                            elif etype == "MODIFIED":
                                pass
                            peak[resource_key] = max(peak[resource_key], current[resource_key])
                except Exception as exc:
                    if stop_event.is_set():
                        return
                    import time
                    time.sleep(1)

        # Start watch threads for each resource type
        v1 = self.k8s_client.CoreV1Api()
        apps_v1 = self.k8s_client.AppsV1Api()

        threads = [
            threading.Thread(target=_watch_resource, args=(v1.list_namespace, "ns"), daemon=True),
            threading.Thread(target=_watch_resource, args=(apps_v1.list_deployment_for_all_namespaces, "dep"), daemon=True),
            threading.Thread(target=_watch_resource, args=(v1.list_service_for_all_namespaces, "svc"), daemon=True),
            threading.Thread(target=_watch_resource, args=(v1.list_config_map_for_all_namespaces, "cm"), daemon=True),
            threading.Thread(target=_watch_resource, args=(v1.list_secret_for_all_namespaces, "sec"), daemon=True),
        ]
        for t in threads:
            t.start()

        # Log progress every 15s until stopped
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=15)
                break
            except asyncio.TimeoutError:
                with lock:
                    total = sum(current.values())
                    if total > 0:
                        log.info("  CL2 objects: ns=%d dep=%d svc=%d cm=%d sec=%d total=%d",
                                 current["ns"], current["dep"], current["svc"],
                                 current["cm"], current["sec"], total)

        # Write peak values to plan
        with lock:
            plan.actual_namespaces = peak["ns"]
            plan.actual_deployments = peak["dep"]
            plan.actual_pods = peak["pod"]
            plan.actual_services = peak["svc"]
            plan.actual_configmaps = peak["cm"]
            plan.actual_secrets = peak["sec"]
            plan.actual_total = sum(peak.values())
            log.info("CL2 peak objects: ns=%d dep=%d svc=%d cm=%d sec=%d total=%d",
                     peak["ns"], peak["dep"], peak["svc"], peak["cm"], peak["sec"],
                     plan.actual_total)

    async def _cleanup_cl2(self) -> None:
        """Remove CL2 preload objects by deleting namespaces prefixed ``cl2-test-``.

        Non-fatal: logs errors but does not fail the test run.
        """
        loop = asyncio.get_event_loop()
        log.info("CL2 cleanup: deleting cl2-test-* namespaces...")
        try:
            def _list_and_delete():
                v1 = self.k8s_client.CoreV1Api()
                namespaces = v1.list_namespace()
                cl2_ns = [
                    ns.metadata.name
                    for ns in namespaces.items
                    if ns.metadata.name.startswith("cl2-test-")
                ]
                deleted = []
                for ns_name in cl2_ns:
                    try:
                        v1.delete_namespace(ns_name)
                        deleted.append(ns_name)
                    except Exception as exc:
                        log.warning(
                            "  Failed to delete namespace %s: %s", ns_name, exc,
                        )
                return deleted

            deleted = await loop.run_in_executor(None, _list_and_delete)
            if deleted:
                log.info(
                    "CL2 cleanup: deleted %d namespaces: %s",
                    len(deleted),
                    ", ".join(deleted),
                )
            else:
                log.info("CL2 cleanup: no cl2-test-* namespaces found")
        except Exception as exc:
            log.warning("CL2 cleanup failed (non-fatal): %s", exc)
