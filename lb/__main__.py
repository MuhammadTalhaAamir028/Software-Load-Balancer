"""Entry point: python -m lb --config config.json

Builds every part through the factories named in the frozen contract, then runs
the proxy, the health checker and the stats server together.
"""
from __future__ import annotations

import argparse
import asyncio

from lb.balancing import make_balancer, make_health_checker, make_pool
from lb.config import load_config
from lb.core.server import serve
from lb.obs.logger import make_logger
from lb.obs.metrics import make_metrics
from lb.obs.stats_server import start_stats_server


async def run(config_path: str) -> None:
    cfg = load_config(config_path)
    log = make_logger(cfg)
    metrics = make_metrics(cfg)
    pool = make_pool(cfg, log, metrics)
    balancer = make_balancer(cfg["algorithm"])
    health = make_health_checker(cfg, pool, log)
    stats = await start_stats_server(cfg, metrics, pool)

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
