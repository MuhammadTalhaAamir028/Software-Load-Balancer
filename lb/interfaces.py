"""Frozen interface contract (tag: lb-contract-v1).

Do NOT edit without a PR that all three members approve.
Standard library only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Sequence


@dataclass
class Backend:
    id: str               # "b1"
    host: str
    port: int
    healthy: bool = True
    active: int = 0       # in-flight requests to this backend


class Balancer(Protocol):
    name: str             # "round_robin" | "least_conn" | "ip_hash"

    def select(self, client_ip: str, backends: Sequence[Backend]) -> Optional[Backend]:
        """backends = HEALTHY only. Return None if empty. Must not block."""


class BackendPool(Protocol):
    def healthy(self) -> list[Backend]: ...          # config order
    def all(self) -> list[Backend]: ...
    def acquire(self, b: Backend) -> None: ...       # active += 1
    def release(self, b: Backend) -> None: ...       # active -= 1
    def set_health(self, backend_id: str, healthy: bool, reason: str) -> None: ...


class HealthChecker(Protocol):
    async def run(self) -> None: ...                 # loops until stop(); calls pool.set_health
    def stop(self) -> None: ...


class EventLogger(Protocol):
    def event(self, name: str, **fields: Any) -> None: ...   # one JSON line, logger adds ts


class Metrics(Protocol):
    def request_done(self, backend_id: Optional[str], status: int, latency_ms: float,
                     bytes_up: int, bytes_down: int, client_ip: str) -> None: ...
    def health_transition(self, backend_id: str, healthy: bool, reason: str) -> None: ...
    def snapshot(self) -> Mapping[str, Any]: ...


# Factories (each owner implements theirs):
#   make_balancer(name) -> Balancer                      Sufyan  lb/balancing
#   make_pool(cfg, log, metrics) -> BackendPool          Sufyan  lb/balancing/pool.py
#   make_health_checker(cfg, pool, log) -> HealthChecker Sufyan  lb/balancing/health.py
#   load_config(path) -> dict                            Sufyan  lb/config.py
#   make_logger(cfg) -> EventLogger                      Abdur   lb/obs/logger.py
#   make_metrics(cfg) -> Metrics                         Abdur   lb/obs/metrics.py
#   async start_stats_server(cfg, metrics, pool)         Abdur   lb/obs/stats_server.py
#   async serve(cfg, balancer, pool, log, metrics)       Talha   lb/core/server.py
