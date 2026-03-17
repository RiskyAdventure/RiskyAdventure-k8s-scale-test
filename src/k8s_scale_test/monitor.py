"""Real-time pod ready rate monitoring via deployment watch + node watch."""

from __future__ import annotations
import asyncio, logging, threading, time as _time
from collections import deque
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
from k8s_scale_test.evidence import EvidenceStore
from k8s_scale_test.models import Alert, AlertType, RateDataPoint, TestConfig

log = logging.getLogger(__name__)

class PodRateMonitor:
    _TICK_INTERVAL = 5.0
    _GAP_THRESHOLD = 2.0

    def __init__(self, config, k8s_client, evidence_store, run_id, namespaces=None):
        self.config, self.k8s_client = config, k8s_client
        self.evidence_store, self.run_id = evidence_store, run_id
        self.namespaces = namespaces or []
        self._running, self._ticker_task, self._threads = False, None, []
        self._time_series, self._alert_callbacks = [], []
        self._window, self._current_rate = deque(), 0.0
        self._lock = threading.Lock()
        self._ready = self._pending = self._total = self._node_count = 0
        self._alert_in_flight, self._ns_counts = False, {}
        self._token_refresh_lock = threading.Lock()
        self._last_token_refresh = 0.0  # monotonic timestamp
        # Tracks the last time a watch thread did a full re-list after reconnect.
        # The ticker uses this to detect that a jump in _ready came from a bulk
        # state refresh rather than real-time events, and marks it as a gap so
        # the rate is computed over the actual disconnection period.
        self._last_relist_time: datetime | None = None

    async def start(self):
        self._running = True
        for ns in self.namespaces:
            t = threading.Thread(target=self._watch_deployments, args=(ns,), daemon=True)
            t.start(); self._threads.append(t)
        t = threading.Thread(target=self._watch_nodes, daemon=True)
        t.start(); self._threads.append(t)
        self._ticker_task = asyncio.create_task(self._ticker_loop())

    async def stop(self):
        self._running = False
        if self._ticker_task:
            self._ticker_task.cancel()
            try: await self._ticker_task
            except asyncio.CancelledError: pass

    def get_current_rate(self): return self._current_rate
    def get_time_series(self): return list(self._time_series)
    def get_counts(self):
        with self._lock: return self._ready, self._pending, self._total
    def get_node_count(self):
        with self._lock: return self._node_count
    def on_alert(self, cb): self._alert_callbacks.append(cb)

    def _is_auth_error(self, exc: Exception) -> bool:
        """Detect K8s API auth failures that need a token refresh."""
        s = str(exc)
        return any(x in s for x in ("Unauthorized", "401", "ExpiredToken"))

    def _try_refresh_token(self) -> bool:
        """Refresh the K8s token if it hasn't been refreshed recently.

        Multiple watch threads may hit auth errors simultaneously.
        The lock + cooldown ensures we only refresh once per 30s window.
        Returns True if a refresh was performed.
        """
        now = _time.monotonic()
        with self._token_refresh_lock:
            if now - self._last_token_refresh < 30.0:
                return False  # another thread already refreshed recently
            if hasattr(self.config, '_k8s_reload'):
                try:
                    self.config._k8s_reload()
                    self._last_token_refresh = _time.monotonic()
                    log.info("Monitor: K8s token refreshed")
                    return True
                except Exception as e:
                    log.warning("Monitor: token refresh failed: %s", e)
            return False

    async def _safe_callback(self, cb, alert):
        """Run alert callback in a separate thread so it never blocks the ticker.

        The anomaly detector's handle_alert does multiple K8s API calls and
        SSM commands that can take 5-10s. Running it on the main event loop
        would starve the ticker. Instead, we spin up a fresh event loop in
        a thread — the callback gets full async support without competing
        with the monitor.
        """
        def _run_in_thread():
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(cb(alert))
            except Exception as e:
                log.error("Alert callback error: %s", e)
            finally:
                loop.close()
                self._alert_in_flight = False

        t = threading.Thread(target=_run_in_thread, daemon=True)
        t.start()

    def _extract(self, dep):
        r = dep.status.replicas or 0; rd = dep.status.ready_replicas or 0
        return rd, r - rd, r

    def _recompute(self):
        ready = pending = total = 0
        for nd in self._ns_counts.values():
            for rd, pd, t in nd.values():
                ready += rd; pending += pd; total += t
        with self._lock:
            self._ready, self._pending, self._total = ready, pending, total

    def _watch_deployments(self, ns):
        from kubernetes import watch as kw
        api = self.k8s_client.AppsV1Api(); w = kw.Watch()
        self._ns_counts[ns] = {}; rv = None
        while self._running:
            try:
                k = {"timeout_seconds": 300}
                if rv: k["resource_version"] = rv; k["allow_watch_bookmarks"] = True
                for ev in w.stream(api.list_namespaced_deployment, ns, **k):
                    if not self._running: w.stop(); return
                    et = ev.get("type")
                    dep = ev.get("object")
                    if dep is None or not hasattr(dep, 'metadata') or dep.metadata is None:
                        continue
                    r = getattr(dep.metadata, 'resource_version', None)
                    if r: rv = r
                    if et == "BOOKMARK": continue
                    nm = dep.metadata.name
                    if et == "DELETED": self._ns_counts[ns].pop(nm, None)
                    else: self._ns_counts[ns][nm] = self._extract(dep)
                    self._recompute()
            except Exception as e:
                if not self._running: return
                if "410" in str(e) or "Gone" in str(e): rv = None
                if self._is_auth_error(e):
                    self._try_refresh_token()
                    api = self.k8s_client.AppsV1Api()
                log.debug("Deploy watch reconnect %s: %s", ns, type(e).__name__)
                try:
                    ds = api.list_namespaced_deployment(ns, watch=False, _request_timeout=10)
                    self._ns_counts[ns] = {d.metadata.name: self._extract(d) for d in ds.items}
                    self._recompute()
                    with self._lock:
                        self._last_relist_time = datetime.now(timezone.utc)
                    if ds.metadata and hasattr(ds.metadata, 'resource_version') and ds.metadata.resource_version:
                        rv = ds.metadata.resource_version
                except Exception: pass
                _time.sleep(2)

    def _watch_nodes(self):
        from kubernetes import watch as kw
        v1 = self.k8s_client.CoreV1Api(); w = kw.Watch(); rv = None
        try:
            nl = v1.list_node(watch=False)
            with self._lock: self._node_count = len(nl.items)
            if nl.metadata and nl.metadata.resource_version: rv = nl.metadata.resource_version
        except Exception: pass
        while self._running:
            try:
                k = {"timeout_seconds": 300}
                if rv: k["resource_version"] = rv; k["allow_watch_bookmarks"] = True
                for ev in w.stream(v1.list_node, **k):
                    if not self._running: w.stop(); return
                    et = ev.get("type")
                    nd = ev.get("object")
                    if nd is None or not hasattr(nd, 'metadata') or nd.metadata is None:
                        continue
                    r = getattr(nd.metadata, 'resource_version', None)
                    if r: rv = r
                    if et == "BOOKMARK": continue
                    with self._lock:
                        if et == "ADDED": self._node_count += 1
                        elif et == "DELETED": self._node_count = max(0, self._node_count - 1)
            except Exception as e:
                if not self._running: return
                if "410" in str(e) or "Gone" in str(e): rv = None
                if self._is_auth_error(e):
                    self._try_refresh_token()
                    v1 = self.k8s_client.CoreV1Api()
                log.debug("Node watch reconnect: %s", type(e).__name__)
                try:
                    nl = v1.list_node(watch=False, _request_timeout=10)
                    with self._lock: self._node_count = len(nl.items)
                    if nl.metadata and hasattr(nl.metadata, 'resource_version') and nl.metadata.resource_version:
                        rv = nl.metadata.resource_version
                except Exception: pass
                _time.sleep(2)

    @staticmethod
    def _compute_rate(p, c, e): return (c - p) / e if e > 0 else 0.0
    def _check_threshold(self, rate, avg):
        return avg > 0 and rate < avg * (1 - self.config.rate_drop_threshold_pct / 100)
    def _rolling_average(self):
        return sum(d.ready_rate for d in self._window) / len(self._window) if self._window else 0.0

    async def _ticker_loop(self):
        prev_ready, prev_time, first = 0, datetime.now(timezone.utc), True
        last_seen_relist: datetime | None = None
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                with self._lock:
                    ready, pending, total = self._ready, self._pending, self._total
                    relist_time = self._last_relist_time
                elapsed = (now - prev_time).total_seconds()
                is_gap = elapsed > self._TICK_INTERVAL * self._GAP_THRESHOLD and not first

                # Detect bulk state jump from a watch reconnect re-list.
                # When a watch thread reconnects and does a full list_namespaced_deployment,
                # _ready can jump by thousands in one shot. The delta belongs to the entire
                # disconnection period, not just the last 5s tick. Mark it as a gap so the
                # rate is attributed correctly.
                relist_gap = False
                if relist_time and relist_time != last_seen_relist and not first:
                    delta = ready - prev_ready
                    if abs(delta) > 100:  # significant jump coinciding with re-list
                        is_gap = True
                        relist_gap = True
                        log.debug("Ticker: relist detected, delta_ready=%d marked as gap", delta)
                    last_seen_relist = relist_time

                # Skip recording during hold-at-peak: pending == 0 and ready is stable
                is_hold = not first and pending == 0 and ready > 0 and ready == prev_ready

                rate = 0.0 if first else self._compute_rate(prev_ready, ready, elapsed)
                if first: first = False
                self._current_rate = rate
                if not is_gap and not is_hold:
                    self._window.append(RateDataPoint(timestamp=now, ready_count=ready,
                        delta_ready=ready-prev_ready, ready_rate=rate, rolling_avg_rate=0.0,
                        pending_count=pending, total_pods=total, interval_seconds=elapsed, is_gap=False))
                cutoff = now.timestamp() - self.config.rolling_avg_window_seconds
                while self._window and self._window[0].timestamp.timestamp() < cutoff: self._window.popleft()
                ra = self._rolling_average()

                # Don't record hold-at-peak data points — they add noise
                if not is_hold:
                    dp = RateDataPoint(timestamp=now, ready_count=ready, delta_ready=ready-prev_ready,
                        ready_rate=rate, rolling_avg_rate=ra, pending_count=pending, total_pods=total,
                        interval_seconds=elapsed, is_gap=is_gap)
                    self._time_series.append(dp)
                    self.evidence_store.append_rate_datapoint(self.run_id, dp)

                # Alert the anomaly detector when a watch reconnect causes a monitoring gap.
                # This lets it investigate what caused the watch to break (API server pressure,
                # token expiry, network issues) rather than silently losing data.
                if relist_gap and not self._alert_in_flight:
                    self._alert_in_flight = True
                    delta = ready - prev_ready
                    a = Alert(alert_type=AlertType.MONITOR_GAP, timestamp=now,
                        message=f"Watch reconnect gap: {elapsed:.0f}s blind spot, delta_ready={delta}",
                        context={"gap_seconds": elapsed, "delta_ready": delta,
                                 "ready": ready, "pending": pending,
                                 "prev_ready": prev_ready,
                                 "namespaces": self.namespaces})
                    log.warning("Monitor gap: %.0fs blind spot, delta_ready=%d (watch reconnect)",
                                elapsed, delta)
                    for cb in self._alert_callbacks: asyncio.create_task(self._safe_callback(cb, a))

                mr = max(100, self.config.target_pods * 0.01)
                if not first and not is_gap and not is_hold and pending > 0 and ready > mr and not self._alert_in_flight and self._check_threshold(rate, ra):
                    self._alert_in_flight = True
                    a = Alert(alert_type=AlertType.RATE_DROP, timestamp=now,
                        message=f"Ready rate {rate:.2f}/s dropped below threshold (rolling avg {ra:.2f}/s)",
                        context={"current_rate": rate, "rolling_avg": ra, "ready": ready, "pending": pending})
                    for cb in self._alert_callbacks: asyncio.create_task(self._safe_callback(cb, a))
                prev_ready, prev_time = ready, now
            except Exception as e: log.error("Ticker error: %s", e)
            await asyncio.sleep(self._TICK_INTERVAL)
