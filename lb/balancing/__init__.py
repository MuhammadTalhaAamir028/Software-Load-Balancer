"""Balancing factories for the load balancer package.

This package exposes factories for balancers, the backend pool, and the
active health checker. The agreed implementation classes are
``RoundRobin``, ``LeastConnections``, ``IPHash``,
``SimpleBackendPool``, and ``ActiveHealthChecker`` in their respective
modules. Failure case: a misspelled algorithm raises ``ValueError`` with the
valid names instead of silently selecting a default.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from lb.interfaces import Balancer, BackendPool, EventLogger, HealthChecker, Metrics

__all__ = ["make_balancer", "make_pool", "make_health_checker"]


def make_balancer(name: str) -> Balancer:
    """Create a new balancer for the named algorithm."""
    valid_names = ("round_robin", "least_conn", "ip_hash")
    if name == "round_robin":
        from .round_robin import RoundRobin

        return RoundRobin()
    if name == "least_conn":
        from .least_conn import LeastConnections

        return LeastConnections()
    if name == "ip_hash":
        from .ip_hash import IPHash

        return IPHash()
    raise ValueError(
        f"Unknown balancing algorithm {name!r}. "
        f"Valid algorithms: {', '.join(valid_names)}"
    )


def make_pool(
    cfg: Mapping[str, Any],
    log: EventLogger,
    metrics: Metrics,
) -> BackendPool:
    """Create the configured backend pool."""
    from .pool import SimpleBackendPool

    return SimpleBackendPool(cfg, log, metrics)


def make_health_checker(
    cfg: Mapping[str, Any],
    pool: BackendPool,
    log: EventLogger,
) -> HealthChecker:
    """Create the configured active health checker."""
    from .health import ActiveHealthChecker

    return ActiveHealthChecker(cfg, pool, log)
