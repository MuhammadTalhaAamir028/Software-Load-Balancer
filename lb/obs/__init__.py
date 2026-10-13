"""Observability package: structured logging, metrics and the stats server.

Public API (exactly the three factories named in the project brief):
    make_logger(cfg)                     -> EventLogger
    make_metrics(cfg)                    -> Metrics
    async start_stats_server(cfg, metrics, pool) -> asyncio.AbstractServer
"""

from lb.obs.logger import make_logger
from lb.obs.metrics import make_metrics
from lb.obs.stats_server import start_stats_server

__all__ = ["make_logger", "make_metrics", "start_stats_server"]