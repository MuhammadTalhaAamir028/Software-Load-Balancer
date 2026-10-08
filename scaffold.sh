#!/usr/bin/env bash
# Project 5 scaffold. Run from the repo root. Idempotent: never overwrites existing files.
set -euo pipefail

mk() { mkdir -p "$1"; }
put() { # put <path> : writes stdin to path only if missing
  if [ -e "$1" ]; then echo "skip   $1"; cat >/dev/null; else mkdir -p "$(dirname "$1")"; cat >"$1"; echo "create $1"; fi
}
keep() { mkdir -p "$1"; [ -e "$1/.gitkeep" ] || touch "$1/.gitkeep"; }

# ---------- directories ----------
for d in lb/core lb/balancing lb/obs/dashboard backends haproxy bench topo captures results docs/briefs tests; do mk "$d"; done
for d in captures results haproxy bench topo backends lb/obs/dashboard; do keep "$d"; done

# ---------- package markers ----------
for f in lb/__init__.py lb/core/__init__.py lb/balancing/__init__.py lb/obs/__init__.py tests/__init__.py; do
  : | put "$f"
done

# ---------- frozen interface contract ----------
put lb/interfaces.py <<'EOF'
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
EOF

# ---------- config example ----------
put config.example.json <<'EOF'
{
  "listen": {"host": "0.0.0.0", "port": 8080},
  "algorithm": "round_robin",
  "backends": [
    {"id": "b1", "host": "10.0.3.11", "port": 8000},
    {"id": "b2", "host": "10.0.3.12", "port": 8000},
    {"id": "b3", "host": "10.0.3.13", "port": 8000},
    {"id": "b4", "host": "10.0.3.14", "port": 8000}
  ],
  "health": {"path": "/health", "interval_s": 1.0, "timeout_s": 0.5, "fail_threshold": 3, "success_threshold": 2},
  "timeouts": {"connect_s": 2.0, "read_s": 10.0, "keepalive_s": 15.0},
  "logging": {"path": "logs/lb.jsonl", "level": "INFO"},
  "stats": {"host": "0.0.0.0", "port": 9090}
}
EOF

# ---------- requirements ----------
put requirements-core.txt <<'EOF'
# lb/ core is Python standard library only. Nothing here on purpose.
EOF
put requirements-dev.txt <<'EOF'
pytest
EOF
put bench/requirements.txt <<'EOF'
psutil
matplotlib
pandas
EOF
put backends/requirements.txt <<'EOF'
flask
EOF

# ---------- notes stubs (each owner fills in) ----------
put docs/notes_core.md <<'EOF'
# Notes: core (Talha)
About 10 plain-English lines per file: what one request does from accept to close, why acquire/release live in a finally, why hop-by-hop headers are stripped, what keep-alive changes, what each error code means. Add a topology note: each route line and what netem does.
EOF
put docs/notes_balancing.md <<'EOF'
# Notes: balancing (Sufyan)
What each algorithm does, why selection is pure, health state machine (3 failures down, 2 successes up), one failure case.
EOF
put docs/notes_obs.md <<'EOF'
# Notes: observability, backends, HAProxy, bench (Abdur Rehman)
What each log event means, how percentiles are computed, how the kill and detection time are measured, one failure case.
EOF

# ---------- docs placeholders ----------
put docs/AI_USE_DISCLOSURE.md <<'EOF'
# AI-use disclosure
Written manually by the group. One general statement, for example: "AI was used to help create X." Get the exact format from the instructor before finalizing.
EOF
put captures/README.md <<'EOF'
# Captures
One section per .pcapng: interface, tcpdump/Wireshark filter used, what it shows.
EOF

# ---------- README ----------
put README.md <<'EOF'
# Software Load Balancer with Health Checks (CS-3001 Project 5)

Python asyncio L7 reverse proxy with round robin, least connections and IP-hash, active health checks, benchmarked against HAProxy on a routed topology.

Team: Talha, Sufyan, Abdur Rehman.

Start here: `docs/PRD.md`, then `docs/RESPONSIBILITIES.md`, then your brief in `docs/briefs/`.

Reproduction steps (setup, run, experiments) are filled in during Phase 4.
EOF

# ---------- .gitignore ----------
put .gitignore <<'EOF'
__pycache__/
*.pyc
.venv/
venv/
.idea/
.vscode/
logs/
*.log
.pytest_cache/
results/raw/
# keep captures/*.pcapng tracked on purpose
EOF

echo
echo "Scaffold done. Review with: git status"
