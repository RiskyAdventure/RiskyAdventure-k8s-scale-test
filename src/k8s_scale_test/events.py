"""Kubernetes event watcher using K8s watch API.

Streams events in real time via watch threads (one per namespace) instead
of polling list_namespaced_event. This avoids massive list responses at
scale and captures events the instant they occur.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable

from k8s_scale_test.evidence import EvidenceStore
from k8s_scale_test.models import Finding, K8sEvent

log = logging.getLogger(__name__)


class EventWatcher:
    """Watch-based K8s event streamer.

    One background thread per namespace watches events and writes them
    to the evidence store as they arrive. No polling, no pagination,
    no 410 Gone errors from stale continue tokens.
    """

    def __init__(
        self,
        k8s_client,
        namespaces: list[str],
        evidence_store: EvidenceStore,
        config=None,
    ) -> None:
        self.k8s_client = k8s_client
        self.namespaces = namespaces
        self.evidence_store = evidence_store
        self.config = config
        self._running = False
        self._threads: list[threading.Thread] = []
        self._run_id: str = ""
        self._step_tracker: Callable[[], int] = lambda: 0
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._token_refresh_lock = threading.Lock()
        self._last_token_refresh = 0.0

    async def start(
        self, run_id: str, scaling_step_tracker: Callable[[], int],
    ) -> None:
        self._run_id = run_id
        self._step_tracker = scaling_step_tracker
        self._running = True
        for ns in self.namespaces:
            t = threading.Thread(target=self._watch_ns, args=(ns,),
                                 daemon=True, name=f"event-watch-{ns}")
            t.start()
            self._threads.append(t)

    async def stop(self) -> None:
        self._running = False
        # Daemon threads die with the process; no join needed

    def _watch_ns(self, namespace: str) -> None:
        """Watch events in a namespace. Runs in a background thread.

        Tracks resource_version so reconnects resume from where we left off
        instead of re-listing all events. This is critical at scale — at 300k
        pods the event list can be enormous.
        """
        from kubernetes import watch as k8s_watch
        import time

        v1 = self.k8s_client.CoreV1Api()
        w = k8s_watch.Watch()
        resource_version = None  # None = start from current state

        while self._running:
            try:
                kwargs = {"timeout_seconds": 300}
                if resource_version:
                    # Resume from last known position — no re-list
                    kwargs["resource_version"] = resource_version
                    kwargs["allow_watch_bookmarks"] = True

                stream = w.stream(
                    v1.list_namespaced_event, namespace, **kwargs)
                for event in stream:
                    if not self._running:
                        w.stop()
                        return

                    etype = event["type"]
                    ev = event["object"]

                    # Track resource_version for reconnect
                    rv = ev.metadata.resource_version
                    if rv:
                        resource_version = rv

                    # Bookmarks are just resource_version markers, no data
                    if etype == "BOOKMARK":
                        continue

                    uid = ev.metadata.uid
                    if not uid:
                        continue

                    with self._lock:
                        if uid in self._seen:
                            continue
                        self._seen.add(uid)

                    ts = ev.last_timestamp or ev.event_time
                    if ts is None:
                        ts = datetime.now(timezone.utc)
                    elif ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)

                    step = self._step_tracker()
                    k8s_ev = K8sEvent(
                        timestamp=ts,
                        namespace=namespace,
                        involved_object_kind=ev.involved_object.kind or "",
                        involved_object_name=ev.involved_object.name or "",
                        reason=ev.reason or "",
                        message=ev.message or "",
                        event_type=ev.type or "Normal",
                        scaling_step=step if ev.type == "Warning" else 0,
                        count=ev.count or 1,
                    )
                    self.evidence_store.append_event(self._run_id, k8s_ev)

            except Exception as exc:
                if self._running:
                    # 410 Gone = resource_version too old, must re-list
                    if "410" in str(exc) or "Gone" in str(exc):
                        log.debug("Event watch %s: resource_version expired, re-listing",
                                  namespace)
                        resource_version = None
                    elif any(x in str(exc) for x in ("Unauthorized", "401", "ExpiredToken")):
                        self._try_refresh_token()
                        v1 = self.k8s_client.CoreV1Api()
                    else:
                        log.debug("Event watch reconnecting for %s: %s",
                                  namespace, type(exc).__name__)
                    time.sleep(2)

    def _try_refresh_token(self) -> None:
        """Refresh K8s token with cooldown to avoid thundering herd."""
        import time as _time
        now = _time.monotonic()
        with self._token_refresh_lock:
            if now - self._last_token_refresh < 30.0:
                return
            if self.config and hasattr(self.config, '_k8s_reload'):
                try:
                    self.config._k8s_reload()
                    self._last_token_refresh = _time.monotonic()
                    log.info("EventWatcher: K8s token refreshed")
                except Exception as e:
                    log.warning("EventWatcher: token refresh failed: %s", e)

    def get_warning_summary(self) -> dict[str, dict]:
        """Return {reason: {count, sample_message, kind}} for all warning reasons.

        Reads from the EvidenceStore's events.jsonl file to get complete event data.
        Groups Warning events by reason and returns count, a sample message, and
        the involved object kind for each reason.

        Returns empty dict if no warning events exist or if events.jsonl is missing.
        """
        import json

        events_path = (
            self.evidence_store._run_dir(self._run_id) / "events.jsonl"
        )
        if not events_path.exists():
            return {}

        summary: dict[str, dict] = {}
        with open(events_path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    log.warning("Skipping malformed JSONL line in events.jsonl")
                    continue

                if ev.get("event_type") != "Warning":
                    continue

                reason = ev.get("reason", "")
                if not reason:
                    continue

                if reason not in summary:
                    summary[reason] = {
                        "count": 0,
                        "sample_message": ev.get("message", ""),
                        "kind": ev.get("involved_object_kind", ""),
                    }
                summary[reason]["count"] += 1

        return summary

    def get_uncovered_reasons(
        self, findings: list[Finding], threshold: int = 100
    ) -> list[tuple[str, dict]]:
        """Return warning reasons with >threshold events not covered by any finding.

        Cross-references warning_summary against findings' evidence_references
        and k8s_events to identify reasons that were never investigated.

        Uses the same cross-referencing logic as chart.py:
        - Checks evidence_references for "warnings:Reason=N" patterns
        - Checks k8s_events for matching reason strings

        Returns list of (reason, info_dict) tuples sorted by count descending.
        """
        summary = self.get_warning_summary()
        if not summary:
            return []

        # Collect all warning reasons mentioned in any finding's evidence,
        # mirroring the logic in chart.py's "Investigated?" column.
        covered_reasons: set[str] = set()
        for f in findings:
            for ev_ref in f.evidence_references:
                if ev_ref.startswith("warnings:"):
                    # Parse "warnings:FailedScheduling=9914,FailedCreatePodSandBox=239"
                    for pair in ev_ref[len("warnings:"):].split(","):
                        reason = pair.split("=")[0]
                        if reason:
                            covered_reasons.add(reason)
            # Also check k8s_events in the finding itself
            for ev in f.k8s_events:
                if ev.reason:
                    covered_reasons.add(ev.reason)

        uncovered = [
            (reason, info)
            for reason, info in summary.items()
            if info["count"] > threshold and reason not in covered_reasons
        ]
        uncovered.sort(key=lambda x: -x[1]["count"])
        return uncovered
