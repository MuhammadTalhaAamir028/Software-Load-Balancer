"""In-memory request and health metrics (module lb.obs.metrics).

Implements the frozen Metrics contract from lb/interfaces.py:

    request_done(backend_id, status, latency_ms, bytes_up, bytes_down, client_ip)
    health_transition(backend_id, healthy, reason)
    snapshot() -> Mapping

All state is bounded. request_done() is O(1): it only increments counters and
appends one number. Sorting for percentiles happens only in snapshot().

Standard library only. Single-threaded use from the asyncio event loop.
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

from lb.obs.logger import utc_iso_ms

# Bound on stored latency samples. After this many requests the percentiles
# describe the most recent MAX_SAMPLES requests only.
MAX_SAMPLES = 100000

# Bound on stored health transitions.
MAX_HEALTH_EVENTS = 100

# A sorted copy of the samples is reused for this many seconds, so a dashboard
# that polls every second does not trigger a new sort on every request.
PERCENTILE_CACHE_S = 1.0


def percentile(sorted_vals: Sequence[float], p: float) -> float:
    """Nearest-rank percentile of an ASCENDING list; p is in (0, 100].

    Returns 0.0 for an empty list. Example: for 1..100, p95 is 95.
    """
    if not sorted_vals:
        return 0.0
    # ceil(p/100 * n) is the 1-based rank of the answer; max(1, ...) protects
    # against rank 0 for very small p.
    k = max(1, math.ceil(p / 100 * len(sorted_vals)))
    return sorted_vals[k - 1]


class _BackendCounters:
    """Counters for one backend. __slots__ keeps each instance small."""

    __slots__ = ("requests", "errors", "bytes_up", "bytes_down")

    def __init__(self) -> None:
        self.requests = 0
        self.errors = 0
        self.bytes_up = 0
        self.bytes_down = 0


class MetricsCollector:
    """Collects counters, latency samples and health events."""

    def __init__(self, max_samples: int = MAX_SAMPLES,
                 percentile_cache_s: float = PERCENTILE_CACHE_S) -> None:
        self.total_requests = 0
        self.errors = 0
        self._backends: Dict[str, _BackendCounters] = {}
        # deque with maxlen drops the oldest item automatically when full.
        self._latencies: Deque[float] = deque(maxlen=max_samples)
        self._health_events: Deque[Dict[str, Any]] = deque(maxlen=MAX_HEALTH_EVENTS)
        # monotonic() cannot jump backwards when the system clock changes,
        # which makes it the right clock for measuring uptime.
        self._start = time.monotonic()
        self._cache_s = percentile_cache_s
        self._cache_at: Optional[float] = None
        self._cache_values: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        # Number of calls that were ignored because of invalid arguments.
        # Kept internal on purpose: the snapshot schema has no extra keys.
        self.internal_errors = 0

    # ------------------------------------------------------------ recording

    def request_done(self, backend_id: Optional[str], status: int, latency_ms: float,
                     bytes_up: int, bytes_down: int, client_ip: str) -> None:
        """Record one finished request. Never raises."""
        try:
            # Convert and validate EVERYTHING first, then change state. If a
            # value is invalid the whole call is ignored, so the counters can
            # never end up half-updated.
            status = int(status)
            latency = float(latency_ms)
            up = int(bytes_up)
            down = int(bytes_down)
        except Exception:
            self.internal_errors += 1
            return

        # Per the brief an error is a 5xx status OR a request that never reached
        # a backend (backend_id is None, for example 503 "no healthy backend").
        is_error = status >= 500 or backend_id is None

        self.total_requests += 1
        if is_error:
            self.errors += 1

        if backend_id is not None:
            counters = self._backends.get(backend_id)
            if counters is None:
                counters = self._backends[backend_id] = _BackendCounters()
            counters.requests += 1
            if status >= 500:
                counters.errors += 1
            counters.bytes_up += up
            counters.bytes_down += down

        # NaN and infinity would poison every percentile, so they are skipped.
        if math.isfinite(latency) and latency >= 0:
            self._latencies.append(latency)

    def health_transition(self, backend_id: str, healthy: bool, reason: str) -> None:
        """Remember a backend going UP or DOWN (kept in a bounded list)."""
        try:
            self._health_events.append({
                "ts": utc_iso_ms(),
                "backend": str(backend_id),
                "healthy": bool(healthy),
                "reason": str(reason),
            })
        except Exception:
            self.internal_errors += 1

    # ------------------------------------------------------------ reporting

    def _percentiles(self) -> Tuple[float, float, float]:
        """Return (p50, p95, p99), re-sorting at most once per cache interval."""
        now = time.monotonic()
        if self._cache_at is not None and (now - self._cache_at) < self._cache_s:
            return self._cache_values
        # sorted() builds a NEW list, so the live deque is not modified.
        ordered = sorted(self._latencies)
        self._cache_values = (percentile(ordered, 50),
                              percentile(ordered, 95),
                              percentile(ordered, 99))
        self._cache_at = now
        return self._cache_values

    def snapshot(self) -> Mapping[str, Any]:
        """Return a JSON-serialisable view of the current state.

        The stats server adds the fields this class cannot know ("algorithm",
        and "active" / "healthy" for each backend, which come from the pool).
        health_events are in chronological order (oldest first).
        """
        p50, p95, p99 = self._percentiles()
        per_backend = {
            backend_id: {
                "requests": c.requests,
                "errors": c.errors,
                "bytes_up": c.bytes_up,
                "bytes_down": c.bytes_down,
            }
            for backend_id, c in self._backends.items()
        }
        return {
            "uptime_s": round(time.monotonic() - self._start, 3),
            "total_requests": self.total_requests,
            "errors": self.errors,
            "latency_ms": {"p50": round(p50, 3), "p95": round(p95, 3),
                           "p99": round(p99, 3)},
            "per_backend": per_backend,
            # Copy each dict so the caller cannot change our stored events.
            "health_events": [dict(e) for e in self._health_events],
        }


def make_metrics(cfg: Mapping[str, Any]) -> MetricsCollector:
    """Factory required by lb/interfaces.py: make_metrics(cfg) -> Metrics.

    cfg is accepted for interface symmetry; no setting is needed (the brief
    forbids adding config keys).
    """
    return MetricsCollector()