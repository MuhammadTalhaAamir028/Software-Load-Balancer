"""Shared backend state for balancing and health checking.

The pool owns the shared ``Backend`` objects and alone records
``backend_down``/``backend_up`` events and health transitions. Failure case:
a double release cannot drive ``active`` negative; it is clamped and logged as
``release_underflow``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from lb.interfaces import Backend

if TYPE_CHECKING:
    from lb.interfaces import BackendPool, EventLogger, Metrics


class SimpleBackendPool:
    """Maintain shared backend objects, health, and active request counts."""

    def __init__(
        self,
        cfg: Mapping[str, Any],
        log: EventLogger,
        metrics: Metrics,
    ) -> None:
        self._log = log
        self._metrics = metrics
        self._backends = [
            Backend(id=entry["id"], host=entry["host"], port=entry["port"])
            for entry in cfg["backends"]
        ]
        self._by_id = {backend.id: backend for backend in self._backends}

    def all(self) -> list[Backend]:
        """Return all shared backends in configuration order."""
        return list(self._backends)

    def healthy(self) -> list[Backend]:
        """Return healthy shared backends in configuration order."""
        return [backend for backend in self._backends if backend.healthy]

    def acquire(self, b: Backend) -> None:
        """Increment a backend's in-flight request count."""
        b.active += 1

    def release(self, b: Backend) -> None:
        """Decrement active requests, clamping underflow and reporting it."""
        if b.active > 0:
            b.active -= 1
            return
        b.active = 0
        try:
            self._log.event(
                "backend_error",
                backend=b.id,
                error="release_underflow",
            )
        except Exception:
            pass

    def set_health(self, backend_id: str, healthy: bool, reason: str) -> None:
        """Change health and emit one ordered transition notification."""
        backend = self._by_id[backend_id]
        if backend.healthy == healthy:
            return
        backend.healthy = healthy
        self._log.event(
            "backend_up" if healthy else "backend_down",
            backend=backend_id,
            reason=reason,
        )
        self._metrics.health_transition(backend_id, healthy, reason)
