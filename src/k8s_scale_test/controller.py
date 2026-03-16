"""Scale Test Controller — orchestrates the full test lifecycle."""

from __future__ import annotations

import asyncio
import logging
import math
import os
from datetime import datetime, timezone
from typing import Optional

from k8s_scale_test.anomaly import AnomalyDetector
from k8s_scale_test.cl2_parser import CL2ResultParser
from k8s_scale_test.diagnostics import NodeDiagnosticsCollector
from k8s_scale_test.evidence import EvidenceStore
from k8s_scale_test.events import EventWatcher
from k8s_scale_test.flux import FluxRepoReader, FluxRepoWriter
from k8s_scale_test.health_sweep import HealthSweepAgent
from k8s_scale_test.infra_health import InfraHealthAgent
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
from k8s_scale_test.preflight import PreflightChecker

log = logging.getLogger(__name__)


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

    async def run(self) -> TestRunSummary:
        """Execute the full test lifecycle.

        1. Preflight capacity validation
        2. Operator approval
        3. Update Flux repo manifests to distribute pods across apps
        4. Git commit + push so Flux reconciles
        5. Monitor pod ready rate, detect anomalies
        6. On completion: update manifests back to 0, commit + push
        """
        start = datetime.now(timezone.utc)
        run_id = self.evidence_store.create_run(self.config)
        self._evidence_run_id = run_id
        log.info("Test run %s started", run_id)

        # 1. Preflight — include CL2 preload pods in capacity check
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

        # 2. Operator approval
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

        # 2a. CL2 preload (if configured) — run concurrently with pod scaling
        cl2_task = None
        if self.config.cl2_preload:
            log.info("CL2 preload: launching concurrently with pod scaling")
            cl2_task = asyncio.create_task(self._run_cl2_preload())

        # 3. Discover deployments and apply role-based filtering
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
        for d in unlabeled:
            log.warning("Deployment %s has no scale-test/role label, excluding from distribution", d.name)

        # Use all labeled deployments for namespace tracking
        all_labeled = stressors + infra
        namespaces = list({d.namespace for d in all_labeled})
        self._namespaces = namespaces

        if not stressors:
            log.warning("No stressor deployments found after filtering")

        # 3c. Scale iperf3 servers first (Task 9.3)
        if infra:
            server_count = max(1, self.config.target_pods // self.config.iperf3_server_ratio)
            infra_targets = [(d.name, server_count, d.source_path) for d in infra]
            writer.set_replicas_batch(infra_targets)
            log.info("Scaling %d infrastructure deployments to %d replicas each",
                     len(infra), server_count)
            await self._git_commit_push("scale-test: deploy iperf3 servers")
            # Wait for servers to be ready before scaling stressors
            log.info("Waiting for infrastructure pods to be ready...")
            for _ in range(60):  # up to 5 minutes
                ready, pending, _ = await self._count_pods()
                if ready >= server_count * len(infra):
                    break
                await asyncio.sleep(5)
            log.info("Infrastructure pods ready, proceeding with stressor scaling")

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

        # 4. Clean slate — delete existing events in target namespaces
        await self._delete_events(namespaces)

        # 5. Set up monitoring components
        monitor = PodRateMonitor(self.config, self.k8s_client, self.evidence_store, run_id, namespaces)
        self._monitor = monitor
        node_metrics = NodeMetricsAnalyzer(self.config, self.k8s_client, self.config.prometheus_url)
        node_diag = NodeDiagnosticsCollector(self.config, self.aws_client, self.evidence_store, run_id)
        anomaly = AnomalyDetector(
            self.config, self.k8s_client, node_metrics, node_diag,
            self.evidence_store, run_id, self._prompt_operator,
            aws_client=self.aws_client,
        )
        sweep_agent = HealthSweepAgent(self.k8s_client, node_diag, self.evidence_store, run_id)
        infra_agent = InfraHealthAgent(self.k8s_client)
        monitor.on_alert(anomaly.handle_alert)
        watcher = EventWatcher(self.k8s_client, namespaces, self.evidence_store)

        # 6. Start watchers
        await watcher.start(run_id, lambda: self._current_step)
        await monitor.start()

        # 7. Scale via Flux repo — update manifests, commit, push
        try:
            result = await self._execute_scaling_via_flux(
                writer, targets, deployments, anomaly,
            )
        except Exception:
            # If scaling fails, still run cleanup — but don't kill the monitor yet
            raise

        # 7. Check Karpenter health if we saw capacity errors during scaling
        self._karpenter_health = await infra_agent.check_if_needed(self._findings)

        # 7. Hold at peak — wait for monitor to confirm target, then hold
        hold = getattr(self.config, 'hold_at_peak', 90)
        target = self.config.target_pods
        log.info("Waiting for monitor to confirm %d pods ready before hold...", target)
        for _ in range(30):  # up to 150s waiting for monitor to catch up
            ready, pending, _ = await self._count_pods()
            if ready >= target:
                break
            await asyncio.sleep(5)

        # 7a. Hold at peak + node health sweep
        # The sweep runs concurrently with the hold timer. We wait for BOTH
        # to complete before cleanup — the sweep must finish so we can analyze
        # results and give the operator a chance to investigate.
        log.info("Holding at peak for %ds + running node health sweep...", hold)
        health_sweep_task = asyncio.create_task(sweep_agent.run(sample_size=10))
        hold_task = asyncio.create_task(asyncio.sleep(hold))

        # Wait for both: hold timer AND sweep completion
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

        # 7c. NOW stop the monitor and watcher — after hold-at-peak is complete
        await monitor.stop()
        await watcher.stop()

        # 8. Cleanup pods — delete replicas and record delete rate
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

        # 8. Summary + chart + verify — after both scale-up AND delete data is collected
        summary = self._make_summary(run_id, start, report, result)
        self.evidence_store.save_summary(run_id, summary)
        self._verify_run_data(run_id, result)

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
        """Git add, commit, push, and trigger Flux reconciliation."""
        import subprocess
        repo = self.config.flux_repo_path
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
                log.warning("Flux reconcile failed (will auto-sync): %s", exc)
        except subprocess.CalledProcessError as exc:
            log.error("Git operation failed: %s\nstderr: %s", exc, exc.stderr.decode() if exc.stderr else "")

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
        """Scale by setting the full target in one commit, then monitor until done.

        One git commit → one Flux reconcile → K8s handles the rest.
        The monitor tracks per-second ready rates continuously.
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

        # Set full target in one commit
        results = writer.set_replicas_batch(targets)
        failed = [n for n, ok in results.items() if not ok]
        if failed:
            log.warning("Failed to update manifests for: %s", failed)

        await self._git_commit_push(f"scale-test: {target} pods")

        # Monitor until target reached or timeout
        start = datetime.now(timezone.utc)
        last_log_time = start
        diag_done = False
        peak_rate = 0.0

        while True:
            await asyncio.sleep(5.0)
            ready, pending, total = await self._count_pods()
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            elapsed_min = elapsed / 60

            # Track peak rate from the monitor (source of truth)
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
                asyncio.create_task(self._run_diagnostics(anomaly, deployments, pending, ready))

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
                finding = await anomaly.handle_alert(alert)
                self._findings.append(finding)
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
            finding = await anomaly.handle_alert(alert)
            self._findings.append(finding)
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

    async def _count_pods(self) -> tuple[int, int, int]:
        """Read pod counts — prefer monitor watch, but always verify with a
        direct API call every 30s to catch stale watch data."""
        # Always do a direct API poll as the source of truth
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
                # Fall back to monitor if API call fails
                if hasattr(self, '_monitor') and self._monitor._running:
                    return self._monitor.get_counts()
                return 0, 0, 0
        return 0, 0, 0

    async def _count_nodes(self) -> int:
        """Read node count from the monitor's watch-maintained counter.
        Falls back to API poll if monitor isn't running yet."""
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

    def _verify_run_data(self, run_id: str, result) -> None:
        """Cross-check rate_data against controller results and observer log."""
        import json
        from pathlib import Path

        rd = self.evidence_store._run_dir(run_id)
        rate_file = rd / "rate_data.jsonl"
        issues = []

        if not rate_file.exists():
            log.warning("Verification: no rate_data.jsonl found")
            return

        points = [json.loads(l) for l in rate_file.read_text().splitlines() if l.strip()]
        if not points:
            log.warning("Verification: rate_data.jsonl is empty")
            return

        # Check 1: data point count vs expected (duration / 5s)
        if result.steps:
            expected_pts = int(result.steps[0].duration_seconds / 5) - 2  # allow margin
            if len(points) < expected_pts:
                issues.append(f"Only {len(points)} data points, expected ~{expected_pts}+ for {result.steps[0].duration_seconds:.0f}s test")

        # Check 2: peak ready in rate_data matches controller result
        rate_peak = max(p.get("ready_count", 0) for p in points)
        if result.total_pods_ready > 0 and abs(rate_peak - result.total_pods_ready) > 100:
            issues.append(f"Peak ready mismatch: rate_data={rate_peak} controller={result.total_pods_ready}")

        # Check 3: gaps
        gaps = [p for p in points if p.get("is_gap")]
        if gaps:
            issues.append(f"{len(gaps)} gap(s) in rate data — chart rates may be averaged over long intervals")

        # Check 4: observer log cross-check
        obs_file = Path("observer.log")
        if obs_file.exists():
            obs_lines = [l.strip() for l in obs_file.read_text().splitlines()
                         if l.strip() and not l.startswith("timestamp") and not l.startswith("Observer")]
            obs_peak = 0
            for line in obs_lines:
                parts = line.split(",", 1)
                if len(parts) < 2:
                    continue
                fields = parts[1].split()
                try:
                    obs_peak = max(obs_peak, int(fields[1]))
                except (ValueError, IndexError):
                    continue
            if obs_peak > 0 and abs(obs_peak - rate_peak) > 500:
                issues.append(f"Observer vs controller mismatch: observer_peak={obs_peak} rate_data_peak={rate_peak}")
            else:
                log.info("Verification: observer cross-check passed (peak=%d)", obs_peak)

        if issues:
            log.warning("Verification issues:")
            for i in issues:
                log.warning("  - %s", i)
        else:
            log.info("Verification: all checks passed (%d points, peak=%d)", len(points), rate_peak)

    @staticmethod
    def _classify_run_validity(
        result: ScalingResult, findings: list[Finding], target: int,
    ) -> tuple[RunValidity, str]:
        """Classify whether the test run is valid.

        Primary criterion: did we reach the target pod count?
        Findings are informational — they don't override a successful scale.
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
                # Find the panic/fatal line first
                for line in result.stderr.strip().split("\n"):
                    if any(k in line for k in ["panic:", "FATAL", "F0", "fatal"]):
                        log.error("  CL2 FATAL: %s", line.strip())
                for line in result.stderr.strip().split("\n")[-20:]:
                    log.warning("  CL2 stderr: %s", line)

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
                log.error("CL2 result parsing failed: %s", exc)
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
