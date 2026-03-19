"""Shared in-memory context for cross-module finding correlation.

The ObservabilityScanner writes ScanResult findings via add() (synchronous).
The AnomalyDetector reads via query() (async-compatible, thread-safe).

Both add() and query() acquire a threading.Lock to protect the shared
_entries list. A threading.Lock is used instead of asyncio.Lock because
add() is called synchronously and query() may run on a different event
loop (diagnostics thread, monitor alert callback thread). An asyncio.Lock
would raise RuntimeError on Python 3.10+ when acquired from a different
event loop than the one it was created on.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone

from k8s_scale_test.models import Alert
from k8s_scale_test.observability import ScanResult, Severity


class SharedContext:
    """In-memory store for cross-module finding correlation.

    Stores timestamped ScanResult entries. Both add() and query()
    acquire a threading.Lock, making the store safe for concurrent
    access from any thread or event loop.
    """

    def __init__(self, max_age_seconds: float = 300.0) -> None:
        self._entries: list[tuple[datetime, ScanResult]] = []
        self._max_age = timedelta(seconds=max_age_seconds)
        self._lock = threading.Lock()

    def add(self, result: ScanResult, timestamp: datetime | None = None) -> None:
        """Add a ScanResult entry. Thread-safe.

        Appends the entry and evicts stale entries older than max_age.
        timestamp defaults to datetime.now(timezone.utc) if not provided.
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        with self._lock:
            self._entries.append((timestamp, result))
            self._evict()

    async def query(
        self, reference_time: datetime, window_seconds: float = 120.0
    ) -> list[tuple[datetime, ScanResult]]:
        """Return entries within ±window_seconds of reference_time.

        Thread-safe. Acquires threading.Lock (not asyncio.Lock) so this
        works correctly when called from any event loop, including the
        separate event loops created by the diagnostics thread and the
        monitor's alert callback.
        """
        window = timedelta(seconds=window_seconds)
        with self._lock:
            results = [
                (ts, sr)
                for ts, sr in self._entries
                if abs(ts - reference_time) <= window
            ]
        results.sort(key=lambda entry: entry[0], reverse=True)
        return results

    def _evict(self) -> None:
        """Remove entries older than max_age. Caller must hold _lock."""
        cutoff = datetime.now(timezone.utc) - self._max_age
        self._entries = [(ts, sr) for ts, sr in self._entries if ts >= cutoff]


# Regex for EC2 internal node names in ScanResult text fields.
_NODE_NAME_RE = re.compile(r"ip-\d+-\d+-\d+-\d+\.\w+\.internal")

# Sort priority: lower value = higher severity = sorts first.
_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.WARNING: 1,
    Severity.INFO: 2,
}


def match_findings(
    scanner_entries: list[tuple[datetime, ScanResult]],
    alert: Alert,
    alert_resources: set[str],
) -> list[tuple[datetime, ScanResult, str]]:
    """Match scanner findings against an alert.

    Returns list of (timestamp, ScanResult, match_type) where
    match_type is "strong" (temporal + resource overlap) or
    "weak" (temporal only).

    Resource extraction: ScanResult stores fleet-aggregated data in
    title and detail strings. Node names matching the pattern
    ``ip-X-X-X-X.<domain>.internal`` are extracted via regex. If the
    extracted node names overlap with *alert_resources* the match is
    "strong"; otherwise it is "weak".

    Results are sorted by severity (CRITICAL first) then timestamp
    (most recent first).
    """
    if not scanner_entries:
        return []

    matches: list[tuple[datetime, ScanResult, str]] = []
    for ts, result in scanner_entries:
        # Extract node names from title and detail.
        text = (result.title or "") + " " + (result.detail or "")
        node_names = set(_NODE_NAME_RE.findall(text))

        if node_names and node_names & alert_resources:
            match_type = "strong"
        else:
            match_type = "weak"

        matches.append((ts, result, match_type))

    # Sort: severity descending (CRITICAL first), then timestamp descending.
    matches.sort(
        key=lambda m: (_SEVERITY_ORDER.get(m[1].severity, 99), -m[0].timestamp()),
    )
    return matches
