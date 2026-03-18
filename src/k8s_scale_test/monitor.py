"""Real-time pod ready rate monitoring via deployment watch + node watch.

Threading model
---------------
This module uses three kinds of concurrency:

1. **Background threads** (``_watch_deployments``, ``_watch_nodes``):
   Each thread opens a long-lived K8s watch stream. Threads are used
   (instead of asyncio) because the ``kubernetes`` Python client's watch
   API is synchronous and blocking. One thread per namespace watches
   Deployment status; one thread watches Node add/delete events.

2. **Async ticker loop** (``_ticker_loop``):
   An ``asyncio.Task`` that wakes every 5 seconds, reads the latest
   counts from the watch threads (via a shared ``threading.Lock``),
   computes the pod-ready rate, records data points, and fires alerts
   when the rate drops below a rolling average threshold.

3. **Alert callbacks** (``_safe_callback``):
   When the ticker fires an alert, the callback (typically the anomaly
   detector) runs in a *new* thread with its own event loop. This
   prevents slow SSM/K8s calls in the anomaly detector from blocking
   the ticker's 5-second cadence.

Data flow::

    K8s API ──watch──▶ _watch_deployments (thread per ns)
                            │  updates _ns_counts dict
                            ▼
                       _recompute()  ◀── _watch_nodes (thread)
                            │                updates _node_count
                            ▼
                       _ticker_loop (async task, every 5s)
                            │  reads _ready, _pending, _total
                            │  computes rate, records data point
                            ▼
                       alert callbacks (separate threads)
"""

from __future__ import annotations
import asyncio, logging, threading, time as _time
from collections import deque
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
from k8s_scale_test.evidence import EvidenceStore
from k8s_scale_test.models import Alert, AlertType, RateDataPoint, TestConfig
from k8s_scale_test.tracing import trace_thread, span

log = logging.getLogger(__name__)

class PodRateMonitor:
    """Tracks how fast pods become Ready across all watched namespaces.

    Combines K8s deployment watches (background threads) with an async
    ticker loop that samples counts every 5 seconds and computes a
    pods-per-second ready rate. Fires alerts when the rate drops
    significantly below its rolling average.

    Attributes
    ----------
    _TICK_INTERVAL : float
        Seconds between each ticker sample (5s).
    _GAP_THRESHOLD : float
        Multiplier on _TICK_INTERVAL. If the elapsed time between two
        ticks exceeds ``_TICK_INTERVAL * _GAP_THRESHOLD``, the interval
        is marked as a "gap" — meaning the ticker was starved or a watch
        reconnected. Gap intervals are excluded from the rolling average
        so they don't distort the rate.
    """

    _TICK_INTERVAL = 5.0
    _GAP_THRESHOLD = 2.0

    def __init__(self, config, k8s_client, evidence_store, run_id, namespaces=None):
        """Initialise the monitor. Does NOT start watching — call ``start()`` for that.

        Parameters
        ----------
        config : TestConfig
            Test configuration (thresholds, target pod count, etc.).
        k8s_client :
            Kubernetes API client (``kubernetes.client`` module).
        evidence_store : EvidenceStore
            Where rate data points are persisted to disk.
        run_id : str
            Current test run identifier.
        namespaces : list[str] | None
            Namespaces to watch. One watch thread is created per namespace.
        """
        self.config, self.k8s_client = config, k8s_client
        self.evidence_store, self.run_id = evidence_store, run_id
        self.namespaces = namespaces or []
        self._running, self._ticker_task, self._threads = False, None, []
        self._time_series, self._alert_callbacks = [], []
        # _window holds recent data points for the rolling average calculation.
        # It's a deque so old points can be efficiently dropped from the left.
        self._window, self._current_rate = deque(), 0.0
        # _lock protects _ready/_pending/_total/_node_count which are written
        # by watch threads and read by the async ticker loop.
        self._lock = threading.Lock()
        self._ready = self._pending = self._total = self._node_count = 0
        # _alert_in_flight prevents multiple concurrent anomaly investigations.
        # Only one alert callback runs at a time to avoid duplicate SSM calls.
        self._alert_in_flight, self._ns_counts = False, {}
        # Token refresh is shared across all watch threads — the lock + cooldown
        # ensures we only call the EKS token refresh API once per 30-second window.
        self._token_refresh_lock = threading.Lock()
        self._last_token_refresh = 0.0  # monotonic timestamp
        # Tracks the last time a watch thread did a full re-list after reconnect.
        # The ticker uses this to detect that a jump in _ready came from a bulk
        # state refresh rather than real-time events, and marks it as a gap so
        # the rate is computed over the actual disconnection period.
        self._last_relist_time: datetime | None = None

    async def start(self):
        """Spin up watch threads and the ticker loop.

        Creates one daemon thread per namespace (deployment watch), one
        daemon thread for node watch, and one daemon thread for the ticker.
        All threads are daemon threads so they die automatically if the
        main process exits.
        """
        self._running = True
        self._event_loop = asyncio.get_event_loop()
        for ns in self.namespaces:
            t = threading.Thread(target=self._watch_deployments, args=(ns,), daemon=True)
            t.start(); self._threads.append(t)
        t = threading.Thread(target=self._watch_nodes, daemon=True)
        t.start(); self._threads.append(t)
        t = threading.Thread(target=self._ticker_thread, daemon=True, name="monitor-ticker")
        t.start(); self._threads.append(t)

    async def stop(self):
        """Signal all threads to stop.

        Sets ``_running = False`` which causes watch threads and the ticker
        thread to exit their loops. All threads are daemon threads so they'll
        be cleaned up even if they don't exit promptly.
        """
        self._running = False

    def get_current_rate(self):
        """Return the most recent pods-per-second ready rate."""
        return self._current_rate

    def get_time_series(self):
        """Return a copy of all recorded ``RateDataPoint`` objects."""
        return list(self._time_series)

    def get_counts(self):
        """Return ``(ready, pending, total)`` pod counts. Thread-safe."""
        with self._lock: return self._ready, self._pending, self._total

    def get_node_count(self):
        """Return the current node count. Thread-safe."""
        with self._lock: return self._node_count

    def on_alert(self, cb):
        """Register an async callback to be invoked when an alert fires.

        Parameters
        ----------
        cb : Callable[[Alert], Awaitable]
            Typically ``anomaly_detector.handle_alert``.
        """
        self._alert_callbacks.append(cb)

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
            with span("anomaly/investigation", alert_type=alert.alert_type.value):
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
        """Pull (ready, pending, total) counts from a Deployment's status.

        Parameters
        ----------
        dep : V1Deployment
            A Kubernetes Deployment object from the watch stream.

        Returns
        -------
        tuple[int, int, int]
            ``(ready_replicas, pending_replicas, total_replicas)``
        """
        r = dep.status.replicas or 0; rd = dep.status.ready_replicas or 0
        return rd, r - rd, r

    def _recompute(self):
        """Sum per-deployment counts across all namespaces into totals.

        Called by watch threads after every event. The lock ensures the
        ticker always sees a consistent snapshot of all three counters.
        """
        ready = pending = total = 0
        for nd in self._ns_counts.values():
            for rd, pd, t in nd.values():
                ready += rd; pending += pd; total += t
        with self._lock:
            self._ready, self._pending, self._total = ready, pending, total

    @trace_thread("watch/deployments")
    def _watch_deployments(self, ns):
        """Watch Deployment status changes in a single namespace (runs in a thread).

        Uses the K8s watch API to stream Deployment events. On each event,
        updates ``_ns_counts[ns]`` with the deployment's ready/pending/total
        counts, then calls ``_recompute()`` to update the global totals.

        Reconnection strategy:
        - On HTTP 410 Gone (resource version too old): resets ``rv`` to None
          so the next iteration does a fresh LIST.
        - On auth errors (401/Unauthorized): refreshes the EKS token and
          recreates the API client.
        - On any other error: does a full re-list to resync state, records
          ``_last_relist_time`` so the ticker can detect the state jump,
          then sleeps 2s before reconnecting.

        Parameters
        ----------
        ns : str
            Kubernetes namespace to watch.
        """
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
                # 410 Gone means our resource_version is too old — must re-list
                if "410" in str(e) or "Gone" in str(e): rv = None
                if self._is_auth_error(e):
                    self._try_refresh_token()
                    api = self.k8s_client.AppsV1Api()
                log.debug("Deploy watch reconnect %s: %s", ns, type(e).__name__)
                # Full re-list to resync state after disconnection
                try:
                    ds = api.list_namespaced_deployment(ns, watch=False, _request_timeout=10)
                    self._ns_counts[ns] = {d.metadata.name: self._extract(d) for d in ds.items}
                    self._recompute()
                    # Signal the ticker that this jump came from a re-list, not
                    # real-time events, so it can mark the interval as a gap.
                    with self._lock:
                        self._last_relist_time = datetime.now(timezone.utc)
                    if ds.metadata and hasattr(ds.metadata, 'resource_version') and ds.metadata.resource_version:
                        rv = ds.metadata.resource_version
                except Exception: pass
                _time.sleep(2)

    @trace_thread("watch/nodes")
    def _watch_nodes(self):
        """Watch Node add/delete events across the cluster (runs in a thread).

        Maintains ``_node_count`` by incrementing on ADDED and decrementing
        on DELETED events. Uses the same reconnection strategy as
        ``_watch_deployments`` (re-list on error, token refresh on auth
        failures).

        Unlike the deployment watch, this doesn't track per-node state —
        it only needs a count. The initial LIST sets the baseline count,
        then the watch stream applies deltas.
        """
        from kubernetes import watch as kw
        v1 = self.k8s_client.CoreV1Api(); w = kw.Watch(); rv = None
        # Initial LIST to establish baseline node count
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
                # Re-list to get accurate count after disconnection
                try:
                    nl = v1.list_node(watch=False, _request_timeout=10)
                    with self._lock: self._node_count = len(nl.items)
                    if nl.metadata and hasattr(nl.metadata, 'resource_version') and nl.metadata.resource_version:
                        rv = nl.metadata.resource_version
                except Exception: pass
                _time.sleep(2)

    @staticmethod
    def _compute_rate(p, c, e):
        """Compute pods-per-second rate: (current - previous) / elapsed."""
        return (c - p) / e if e > 0 else 0.0

    def _check_threshold(self, rate, avg):
        """Return True if ``rate`` has dropped below the configured percentage of ``avg``.

        For example, if ``rate_drop_threshold_pct`` is 50 and the rolling
        average is 10/s, this returns True when rate < 5/s.
        """
        return avg > 0 and rate < avg * (1 - self.config.rate_drop_threshold_pct / 100)

    def _rolling_average(self):
        """Compute the mean ready rate over the rolling window.

        The window size is controlled by ``config.rolling_avg_window_seconds``.
        Old data points are pruned in the ticker loop before this is called.
        """
        return sum(d.ready_rate for d in self._window) / len(self._window) if self._window else 0.0

    @trace_thread("ticker")
    def _ticker_thread(self):
        """Main sampling loop — runs in its own thread, sleeps every 5 seconds.

        Completely independent of the asyncio event loop. This ensures that
        even if the event loop is blocked by investigation work, rate data
        points are still recorded on schedule.

        Each tick:
        1. Reads the latest counts from the watch threads (thread-safe via lock).
        2. Computes the instantaneous ready rate (delta_ready / elapsed_seconds).
        3. Detects "gaps" — intervals where the ticker was starved or a watch
           thread reconnected and did a bulk re-list. Gap intervals are excluded
           from the rolling average to avoid distorting the rate.
        4. Detects "hold" — when pending == 0 and ready is stable, meaning
           we've reached the target. Hold data points are not recorded because
           they add noise (rate is 0 but that's expected, not an anomaly).
        5. Updates the rolling average window (drops points older than
           ``config.rolling_avg_window_seconds``).
        6. Fires alerts when:
           - A watch reconnect caused a monitoring gap (MONITOR_GAP alert).
           - The ready rate drops below the rolling average threshold
             (RATE_DROP alert), but only when we're not near the target
             (the rate naturally drops to zero as the last pods come online).
        """
        prev_ready, prev_time, first = 0, datetime.now(timezone.utc), True
        last_seen_relist: datetime | None = None
        while self._running:
          with span("monitor/tick"):
            try:
                now = datetime.now(timezone.utc)
                with self._lock:
                    ready, pending, total = self._ready, self._pending, self._total
                    relist_time = self._last_relist_time
                elapsed = (now - prev_time).total_seconds()
                is_gap = elapsed > self._TICK_INTERVAL * self._GAP_THRESHOLD and not first

                # Detect bulk state jump from a watch reconnect re-list.
                relist_gap = False
                if relist_time and relist_time != last_seen_relist and not first:
                    delta = ready - prev_ready
                    if abs(delta) > 100:
                        is_gap = True
                        relist_gap = True
                        log.debug("Ticker: relist detected, delta_ready=%d marked as gap", delta)
                    last_seen_relist = relist_time

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

                if not is_hold:
                    dp = RateDataPoint(timestamp=now, ready_count=ready, delta_ready=ready-prev_ready,
                        ready_rate=rate, rolling_avg_rate=ra, pending_count=pending, total_pods=total,
                        interval_seconds=elapsed, is_gap=is_gap)
                    self._time_series.append(dp)
                    self.evidence_store.append_rate_datapoint(self.run_id, dp)

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
                    self._dispatch_alert(a)

                mr = max(100, self.config.target_pods * 0.01)
                near_target = self.config.target_pods > 0 and ready >= self.config.target_pods * 0.95
                if not first and not is_gap and not is_hold and not near_target and pending > 0 and ready > mr and not self._alert_in_flight and self._check_threshold(rate, ra):
                    self._alert_in_flight = True
                    a = Alert(alert_type=AlertType.RATE_DROP, timestamp=now,
                        message=f"Ready rate {rate:.2f}/s dropped below threshold (rolling avg {ra:.2f}/s)",
                        context={"current_rate": rate, "rolling_avg": ra, "ready": ready, "pending": pending})
                    self._dispatch_alert(a)
                prev_ready, prev_time = ready, now
            except Exception as e: log.error("Ticker error: %s", e)
          _time.sleep(self._TICK_INTERVAL)

    def _dispatch_alert(self, alert):
        """Fire alert callbacks from the ticker thread.

        Each callback runs in its own thread (via _safe_callback) so it
        never blocks the ticker. We use call_soon_threadsafe to schedule
        the callback dispatch on the main event loop.
        """
        for cb in self._alert_callbacks:
            try:
                self._event_loop.call_soon_threadsafe(
                    asyncio.ensure_future,
                    self._safe_callback(cb, alert),
                )
            except RuntimeError:
                # Event loop closed — test is shutting down
                pass
