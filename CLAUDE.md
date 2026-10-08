# CLAUDE.md: Project 5 Software Load Balancer (CS-3001)

Python asyncio L7 reverse proxy (HTTP/1.1) with round robin, least connections, IP-hash and active health checks. Benchmarked against HAProxy on a routed topology. Team: Talha, Sufyan, Abdur Rehman. Address people by name.

## Read first
1. `docs/PRD.md`: scope, locked decisions (D1-D14), addressing, ports, interface contract, experiment plan.
2. `docs/RESPONSIBILITIES.md`: who owns which files.
3. `docs/briefs/` : the owner's brief, if present.

## Hard rules
- Scope = official Project 5 only. No extra features. Allowed optional extras only: dynamic weights, consistent hashing, connection draining, TLS termination, L4-vs-L7 comparison. No connection pooling, caching, auth, filtering, rate limiting.
- `lb/interfaces.py` is frozen (tag `lb-contract-v1`). Do not edit it. If it blocks you, write the problem down and tell the group.
- `lb/` is Python standard library only. Third-party packages only in `bench/` and `backends/`.
- No globals, no singletons, no hard-coded IPs or ports. Config dict or arguments only. Dependencies are injected.
- Balancers are pure: `select(client_ip, healthy_backends) -> Backend | None`. No I/O.
- `pool.healthy()` returns backends in config order.
- Python 3.9+.
- Git: branches and PRs only. Never push to `main`. Branches: `feat/lb-core` (Talha), `feat/lb-balancing` (Sufyan), `feat/lb-obs` (Abdur). Topology work: `feat/topo`.
- Each owner writes unit tests for their own module in `tests/` and a plain-English `docs/notes_*.md`.
- Never fabricate results. Every number in the report comes from a scripted run with logs and packet traces.
- Do not run experiments on localhost. They run on the routed topology in `topo/`.

## Layout
```
lb/interfaces.py   frozen contract
lb/config.py       Sufyan
lb/core/           Talha: server.py, http_io.py, forward.py
lb/balancing/      Sufyan: round_robin.py, least_conn.py, ip_hash.py, pool.py, health.py
lb/obs/            Abdur: logger.py, metrics.py, stats_server.py, dashboard/
backends/          Abdur
haproxy/           Abdur
bench/             Abdur
topo/              Talha: containerlab + FRR, routes, netem scripts
captures/          Talha: .pcapng + README.md
results/           run outputs
docs/              PRD, responsibilities, briefs, notes, report, disclosure
tests/
```

## Environment
- Linux only for topology and experiments (WSL2 Ubuntu or VM). Windows is fine for editing code and running `lb/core` unit tests.
- Work from the Linux filesystem (`~/`), not `/mnt/c/...`.
- Tools: containerlab + FRR, Docker, HAProxy, wrk or hey, tcpdump, Wireshark, tc netem.
- Set `ulimit -n 65535` before 500-connection runs.

## Frozen numbers
- Health: GET /health every 1 s, timeout 0.5 s, DOWN after 3 consecutive failures, re-admit after 2 consecutive successes.
- Ports: LB 8080, HAProxy 8080 (different IP), HAProxy stats 8404, backend 8000, LB stats 9090.
- Subnets: clients 10.0.1.0/24, LB segment 10.0.2.0/24 (bridge), backends 10.0.3.0/24.
- Test: 50/200/500 concurrency, 60 s, kill b3 with `docker kill` at t=30 s, 3 runs each, 5 s warm-up discarded.

## Working style
- Explain what each block does in plain English. The group is learning while building.
- Keep summaries short. Overview first.
- Ask before changing anything in `docs/PRD.md` or the contract.
