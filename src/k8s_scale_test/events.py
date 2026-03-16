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
from k8s_scale_test.models import K8sEvent

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
    ) -> None:
        self.k8s_client = k8s_client
        self.namespaces = namespaces
        self.evidence_store = evidence_store
        self._running = False
        self._threads: list[threading.Thread] = []
        self._run_id: str = ""
        self._step_tracker: Callable[[], int] = lambda: 0
        self._seen: set[str] = set()
        self._lock = threading.Lock()

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
                    else:
                        log.debug("Event watch reconnecting for %s: %s",
                                  namespace, type(exc).__name__)
                    time.sleep(2)
