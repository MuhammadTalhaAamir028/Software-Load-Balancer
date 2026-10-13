"""Entry point: python -m lb --config config.json

Builds every part through the factories named in the frozen contract, then runs
the proxy, the health checker and the stats server together.
Factories are imported lazily so a missing teammate module gives a clear message.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from typing import Any, Callable

from lb.core.server import serve

# factory name -> module that must provide it (see "Factories" in docs/PRD.md section 7)
FACTORIES = {
    "load_config": "lb.config",
    "make_balancer": "lb.balancing",
    "make_pool": "lb.balancing",
    "make_health_checker": "lb.balancing",
    "make_logger": "lb.obs.logger",
    "make_metrics": "lb.obs.metrics",
    "start_stats_server": "lb.obs.stats_server",
}


def _factory(name: str) -> Callable[..., Any]:
    module = FACTORIES[name]
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as e:
        sys.exit(f"cannot start: {name} not found in {module} ({e}). "
                 f"That module belongs to a teammate and has not landed yet.")


async def run(config_path: str) -> None:
    cfg = _factory("load_config")(config_path)
    log = _factory("make_logger")(cfg)
    metrics = _factory("make_metrics")(cfg)
    pool = _factory("make_pool")(cfg, log, metrics)
    balancer = _factory("make_balancer")(cfg["algorithm"])
    health = _factory("make_health_checker")(cfg, pool, log)
    stats = await _factory("start_stats_server")(cfg, metrics, pool)

    health_task = asyncio.create_task(health.run())
    try:
        await serve(cfg, balancer, pool, log, metrics)
    finally:
        health.stop()
        await asyncio.gather(health_task, return_exceptions=True)
        stats.close()
        await stats.wait_closed()


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m lb", description="Software load balancer")
    ap.add_argument("--config", required=True, help="path to config.json")
    args = ap.parse_args()
    try:
        asyncio.run(run(args.config))
    except KeyboardInterrupt:
        pass  # Windows Ctrl+C; serve() already shut down in its finally block


if __name__ == "__main__":
    main()
