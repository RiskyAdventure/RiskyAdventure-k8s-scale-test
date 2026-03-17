"""Continuous observability scanning during scale tests.

Runs a periodic scan loop during scaling and hold-at-peak phases,
using the MCP servers (AMP, CloudWatch, EKS) to detect problems
before they cause pod ready rate drops.

Design principles:
- Phase-aware: different queries run during different test phases
- Tiered: Prometheus for broad sweeps, CloudWatch/EKS for drill-down
- Catalog-driven: queries are data, not hardcoded logic
- Adaptive: skips queries that aren't relevant to current conditions
- Non-blocking: runs in a background task, never stalls the controller
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Awaitable

log = logging.getLogger(__name__)


class Phase(Enum):
    SCALING = "scaling"
    HOLD_AT_PEAK = "hold-at-peak"


class Severity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Source(Enum):
    PROMETHEUS = "prometheus"
    CLOUDWATCH = "cloudwatch"
    EKS = "eks"


@dataclass
class ScanQuery:
    """A single observability query in the catalog."""
    name: str
    source: Source
    query: str  # PromQL, CW Insights query string, or EKS resource spec
    phases: list[Phase]  # When this query is relevant
    description: str
    # Condition function: receives current context, returns True if query should run.
    # This allows queries to be skipped when they're not relevant (e.g., no point
    # checking Karpenter if node count isn't growing).
    condition: Callable[[dict], bool] = field(default=lambda: (lambda ctx: True))
    # Evaluator: receives the raw query result and context, returns a ScanResult
    # or None if the result is normal.
    evaluate: Callable[[Any, dict], "ScanResult | None"] = field(
        default=lambda: (lambda result, ctx: None)
    )
    # Interval override — some queries should run more/less frequently
    interval_seconds: float = 30.0


@dataclass
class ScanResult:
    """Output from a scan query evaluation."""
    query_name: str
    severity: Severity
    title: str
    detail: str
    source: Source
    raw_result: Any = None
    # If set, triggers a drill-down into a different source
    drill_down_source: Source | None = None
    drill_down_query: str | None = None



def _default_catalog() -> list[ScanQuery]:
    """Built-in query catalog. Covers the most common scale test signals.

    Each query is tagged with the phases where it's relevant and a condition
    function that checks the current context before running. Evaluators
    interpret the raw result and decide whether it's anomalous.

    The catalog is intentionally broad — conditions and phase tags keep
    irrelevant queries from running. Add new queries here without touching
    the scanner loop.
    """
    return [
        # --- Node provisioning (Prometheus) ---
        ScanQuery(
            name="node_count",
            source=Source.PROMETHEUS,
            query="count(kube_node_info)",
            phases=[Phase.SCALING],
            description="Track node count growth during scaling",
            interval_seconds=15.0,
            evaluate=lambda result, ctx: _eval_node_growth(result, ctx),
        ),
        ScanQuery(
            name="node_not_ready",
            source=Source.PROMETHEUS,
            query='count(kube_node_status_condition{condition="Ready",status="false"})',
            phases=[Phase.SCALING, Phase.HOLD_AT_PEAK],
            description="Nodes in NotReady state",
            evaluate=lambda result, ctx: _eval_threshold(
                result, ctx, "node_not_ready", 10,
                "NotReady nodes", Severity.WARNING,
                drill_down=Source.EKS,
            ),
        ),

        # --- Pod lifecycle (Prometheus) ---
        ScanQuery(
            name="pending_pods",
            source=Source.PROMETHEUS,
            query='sum(kube_pod_status_phase{phase="Pending",namespace="stress-test"})',
            phases=[Phase.SCALING],
            description="Pending pod count — high values indicate scheduling bottleneck",
            interval_seconds=15.0,
            condition=lambda ctx: ctx.get("elapsed_minutes", 0) > 2,
            evaluate=lambda result, ctx: _eval_pending_ratio(result, ctx),
        ),
        ScanQuery(
            name="pod_restarts",
            source=Source.PROMETHEUS,
            query='sum(increase(kube_pod_container_status_restarts_total{namespace="stress-test"}[5m]))',
            phases=[Phase.SCALING, Phase.HOLD_AT_PEAK],
            description="Pod restart rate — indicates crashloops or OOM kills",
            evaluate=lambda result, ctx: _eval_threshold(
                result, ctx, "pod_restarts", 50,
                "Pod restarts in last 5m", Severity.WARNING,
                drill_down=Source.CLOUDWATCH,
            ),
        ),

        # --- Resource pressure (Prometheus) ---
        ScanQuery(
            name="cpu_pressure",
            source=Source.PROMETHEUS,
            query='avg(100 - (rate(node_cpu_seconds_total{mode="idle"}[2m]) * 100))',
            phases=[Phase.SCALING, Phase.HOLD_AT_PEAK],
            description="Fleet average CPU utilization",
            evaluate=lambda result, ctx: _eval_threshold(
                result, ctx, "cpu_pressure", 90,
                "Fleet avg CPU", Severity.WARNING,
            ),
        ),
        ScanQuery(
            name="memory_pressure",
            source=Source.PROMETHEUS,
            query="avg(100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))",
            phases=[Phase.HOLD_AT_PEAK],
            description="Fleet average memory utilization",
            evaluate=lambda result, ctx: _eval_threshold(
                result, ctx, "memory_pressure", 80,
                "Fleet avg memory", Severity.WARNING,
            ),
        ),
        ScanQuery(
            name="cpu_outliers",
            source=Source.PROMETHEUS,
            query='count(100 - (rate(node_cpu_seconds_total{mode="idle"}[2m]) * 100) > 95)',
            phases=[Phase.SCALING, Phase.HOLD_AT_PEAK],
            description="Nodes with CPU > 95% — potential hotspots",
            evaluate=lambda result, ctx: _eval_threshold(
                result, ctx, "cpu_outliers", 5,
                "Nodes with CPU > 95%", Severity.INFO,
            ),
        ),

        # --- Network / CNI (Prometheus) ---
        ScanQuery(
            name="network_errors",
            source=Source.PROMETHEUS,
            query="sum(rate(node_network_receive_errs_total[2m]) + rate(node_network_transmit_errs_total[2m]))",
            phases=[Phase.SCALING, Phase.HOLD_AT_PEAK],
            description="Fleet-wide network error rate",
            evaluate=lambda result, ctx: _eval_threshold(
                result, ctx, "network_errors", 10,
                "Network errors/s", Severity.WARNING,
                drill_down=Source.CLOUDWATCH,
            ),
        ),

        # --- Karpenter health (Prometheus) ---
        ScanQuery(
            name="karpenter_queue_depth",
            source=Source.PROMETHEUS,
            query='sum(karpenter_provisioner_scheduling_queue_depth) or vector(0)',
            phases=[Phase.SCALING],
            description="Karpenter scheduling queue depth — backlog of unscheduled pods",
            interval_seconds=15.0,
            evaluate=lambda result, ctx: _eval_threshold(
                result, ctx, "karpenter_queue_depth", 1000,
                "Karpenter queue depth", Severity.WARNING,
            ),
        ),
        ScanQuery(
            name="karpenter_errors",
            source=Source.PROMETHEUS,
            query='sum(increase(karpenter_cloudprovider_errors_total[2m])) or vector(0)',
            phases=[Phase.SCALING],
            description="Karpenter cloud provider errors — EC2 API failures",
            evaluate=lambda result, ctx: _eval_threshold(
                result, ctx, "karpenter_errors", 5,
                "Karpenter cloud provider errors in 2m", Severity.WARNING,
                drill_down=Source.CLOUDWATCH,
            ),
        ),

        # --- Disk (Prometheus) ---
        ScanQuery(
            name="disk_pressure",
            source=Source.PROMETHEUS,
            query='count(node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"} < 0.1)',
            phases=[Phase.SCALING, Phase.HOLD_AT_PEAK],
            description="Nodes with < 10% disk free",
            evaluate=lambda result, ctx: _eval_threshold(
                result, ctx, "disk_pressure", 1,
                "Nodes with < 10% disk free", Severity.WARNING,
            ),
        ),

        # --- CloudWatch drill-down (only triggered by Prometheus findings) ---
        ScanQuery(
            name="cw_top_errors",
            source=Source.CLOUDWATCH,
            query=(
                "fields @timestamp, @message"
                " | filter @message like /(?i)error|fail/"
                " | stats count() as cnt by @message"
                " | sort cnt desc"
                " | limit 10"
            ),
            phases=[Phase.SCALING, Phase.HOLD_AT_PEAK],
            description="Top 10 error patterns in dataplane logs",
            interval_seconds=60.0,
            # Only run when triggered by a Prometheus finding or periodically
            condition=lambda ctx: ctx.get("has_prometheus_finding", False)
                or ctx.get("scan_count", 0) % 4 == 0,
            evaluate=lambda result, ctx: _eval_cw_errors(result, ctx),
        ),
    ]


# --- Evaluator functions ---

def _extract_scalar(result: dict) -> float | None:
    """Extract a single scalar value from a Prometheus instant query result."""
    if not isinstance(result, dict):
        return None
    data = result.get("result", [])
    if not data:
        return None
    try:
        return float(data[0].get("value", [None, None])[1])
    except (TypeError, ValueError, IndexError):
        return None


def _eval_threshold(
    result: dict, ctx: dict, name: str, threshold: float,
    label: str, severity: Severity,
    drill_down: Source | None = None,
) -> ScanResult | None:
    val = _extract_scalar(result)
    if val is None or val <= threshold:
        return None
    return ScanResult(
        query_name=name, severity=severity,
        title=f"{label}: {val:.1f} (threshold: {threshold})",
        detail=f"{label} is {val:.1f}, exceeding threshold of {threshold}",
        source=Source.PROMETHEUS, raw_result=result,
        drill_down_source=drill_down,
    )


def _eval_node_growth(result: dict, ctx: dict) -> ScanResult | None:
    """Check if node count is growing as expected during scaling."""
    val = _extract_scalar(result)
    if val is None:
        return None
    prev = ctx.get("prev_node_count", 0)
    elapsed = ctx.get("elapsed_minutes", 0)
    ctx["prev_node_count"] = val
    # Only alert if nodes stopped growing while pods are still pending
    if prev > 0 and val == prev and elapsed > 3 and ctx.get("pending", 0) > 1000:
        return ScanResult(
            query_name="node_count", severity=Severity.WARNING,
            title=f"Node count stalled at {int(val)} with {ctx.get('pending', 0)} pods pending",
            detail=f"Node count has not increased from {int(val)} in the last scan interval "
                   f"while {ctx.get('pending', 0)} pods are still pending. "
                   f"Karpenter may be blocked or EC2 capacity exhausted.",
            source=Source.PROMETHEUS, raw_result=result,
            drill_down_source=Source.CLOUDWATCH,
        )
    return None


def _eval_pending_ratio(result: dict, ctx: dict) -> ScanResult | None:
    """Alert if pending pods are a high fraction of target after initial ramp."""
    val = _extract_scalar(result)
    if val is None:
        return None
    target = ctx.get("target_pods", 30000)
    elapsed = ctx.get("elapsed_minutes", 0)
    # After 5 minutes, more than 60% pending suggests a bottleneck
    if elapsed > 5 and val > target * 0.6:
        return ScanResult(
            query_name="pending_pods", severity=Severity.WARNING,
            title=f"{int(val)} pods still pending after {elapsed:.0f}m ({val/target*100:.0f}% of target)",
            detail=f"After {elapsed:.0f} minutes of scaling, {int(val)}/{target} pods are still "
                   f"Pending. Expected most pods to be scheduled by now.",
            source=Source.PROMETHEUS, raw_result=result,
            drill_down_source=Source.EKS,
        )
    return None


def _eval_cw_errors(result: dict, ctx: dict) -> ScanResult | None:
    """Summarize top CloudWatch error patterns."""
    results = result.get("results", [])
    if not results:
        return None
    top = results[:5]
    lines = [f"  {r.get('cnt', '?')}x: {r.get('@message', '?')[:120]}" for r in top]
    return ScanResult(
        query_name="cw_top_errors", severity=Severity.INFO,
        title=f"Top {len(top)} error patterns in dataplane logs",
        detail="\n".join(lines),
        source=Source.CLOUDWATCH, raw_result=result,
    )


class ObservabilityScanner:
    """Runs periodic observability scans during scale tests.

    Usage:
        scanner = ObservabilityScanner(config, prometheus_fn, cloudwatch_fn, eks_fn)
        scanner.set_phase(Phase.SCALING)
        task = asyncio.create_task(scanner.run())
        # ... scaling happens ...
        scanner.set_phase(Phase.HOLD_AT_PEAK)
        # ... hold happens ...
        await scanner.stop()
        findings = scanner.get_findings()

    The scanner is decoupled from the MCP servers — it takes query executor
    functions that the controller provides. This keeps the scanner testable
    and allows swapping in mock executors for unit tests.
    """

    def __init__(
        self,
        config,
        prometheus_fn: Callable[[str], Awaitable[dict]] | None = None,
        cloudwatch_fn: Callable[[str, str, str], Awaitable[dict]] | None = None,
        catalog: list[ScanQuery] | None = None,
    ):
        self.config = config
        self._prometheus_fn = prometheus_fn
        self._cloudwatch_fn = cloudwatch_fn
        self._catalog = catalog or _default_catalog()
        self._phase: Phase | None = None
        self._running = False
        self._findings: list[ScanResult] = []
        self._context: dict = {
            "target_pods": getattr(config, "target_pods", 30000),
            "scan_count": 0,
            "prev_node_count": 0,
        }
        self._on_finding: Callable[[ScanResult], None] | None = None
        self._last_run: dict[str, float] = {}  # query_name -> last run timestamp

    def set_phase(self, phase: Phase) -> None:
        self._phase = phase
        log.info("ObservabilityScanner: phase → %s", phase.value)

    def update_context(self, **kwargs) -> None:
        """Update scan context with current controller state."""
        self._context.update(kwargs)

    def on_finding(self, callback: Callable[[ScanResult], None]) -> None:
        """Register a callback for new findings."""
        self._on_finding = callback

    def get_findings(self) -> list[ScanResult]:
        return list(self._findings)

    async def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Main scan loop. Runs until stop() is called."""
        self._running = True
        log.info("ObservabilityScanner: started with %d queries in catalog", len(self._catalog))

        while self._running:
            if self._phase is None:
                await asyncio.sleep(5)
                continue

            self._context["scan_count"] = self._context.get("scan_count", 0) + 1
            self._context["has_prometheus_finding"] = False
            now = asyncio.get_event_loop().time()

            # Select queries for current phase that are due to run
            due_queries = []
            for q in self._catalog:
                if self._phase not in q.phases:
                    continue
                last = self._last_run.get(q.name, 0)
                if now - last < q.interval_seconds:
                    continue
                try:
                    if not q.condition(self._context):
                        continue
                except Exception:
                    continue
                due_queries.append(q)

            if not due_queries:
                await asyncio.sleep(5)
                continue

            # Run Prometheus queries concurrently, CloudWatch sequentially
            prom_queries = [q for q in due_queries if q.source == Source.PROMETHEUS]
            cw_queries = [q for q in due_queries if q.source == Source.CLOUDWATCH]

            # Execute Prometheus queries
            if prom_queries and self._prometheus_fn:
                prom_tasks = [
                    self._run_query(q, now) for q in prom_queries
                ]
                await asyncio.gather(*prom_tasks, return_exceptions=True)

            # If any Prometheus finding triggered, mark context for CloudWatch
            if any(f.source == Source.PROMETHEUS for f in self._findings[-len(prom_queries):] if f):
                self._context["has_prometheus_finding"] = True

            # Execute CloudWatch queries
            if cw_queries and self._cloudwatch_fn:
                for q in cw_queries:
                    await self._run_query(q, now)

            await asyncio.sleep(5)

        log.info("ObservabilityScanner: stopped with %d findings", len(self._findings))

    async def _run_query(self, query: ScanQuery, now: float) -> None:
        """Execute a single query and evaluate the result."""
        try:
            self._last_run[query.name] = now

            if query.source == Source.PROMETHEUS and self._prometheus_fn:
                result = await self._prometheus_fn(query.query)
            elif query.source == Source.CLOUDWATCH and self._cloudwatch_fn:
                # CloudWatch needs time window — use last 5 minutes
                end = datetime.now(timezone.utc)
                start_iso = (end.replace(second=0, microsecond=0)).isoformat()
                from datetime import timedelta
                start = end - timedelta(minutes=5)
                result = await self._cloudwatch_fn(
                    query.query, start.isoformat(), end.isoformat()
                )
            else:
                return

            # Evaluate
            finding = query.evaluate(result, self._context)
            if finding:
                self._findings.append(finding)
                log.warning("Scan [%s] %s: %s", query.name, finding.severity.value, finding.title)
                if self._on_finding:
                    try:
                        self._on_finding(finding)
                    except Exception:
                        pass
            else:
                log.debug("Scan [%s]: normal", query.name)

        except Exception as exc:
            log.debug("Scan [%s] failed: %s", query.name, exc)
