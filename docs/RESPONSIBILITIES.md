# Team Responsibilities: Project 5 Load Balancer

Course: Computer Networks (CS-3001). Team: Talha, Sufyan, Abdur Rehman.

## Ownership summary
| Member | Owns |
|---|---|
| Talha | Asyncio server core, HTTP parsing/forwarding, keep-alive, timeouts, topology + routing + netem, packet captures, architecture diagram |
| Sufyan | Three algorithms, backend pool, health checker (removal/re-admission), config loader, unit tests for all |
| Abdur Rehman | Logger, metrics, stats server and dashboard UI, backend servers, HAProxy config, load scripts, analysis, plots |
| All | Proposal, README, report, AI-use disclosure (written manually, one general statement) |

## Talha
Files:
```
lb/core/server.py, http_io.py, forward.py
lb/__main__.py            wires everything from a config file
topo/build.sh (or clab.yml), routes.sh, set_rtt.sh <ms>, set_loss.sh <pct>, teardown.sh
captures/                 pcapng + captures/README.md
docs/architecture.*       diagram: subnets, IPs, ports, flows, bridge on 10.0.2.0/24
docs/notes_core.md
tests/test_core.py
```
Delivers:
- `serve(cfg, balancer, pool, log, metrics)`: accept, parse, select, acquire, forward, relay, release in `finally`.
- HTTP/1.1 subset: GET/HEAD/POST/PUT/DELETE, Content-Length bodies, X-Forwarded-For, hop-by-hop headers handled, client keep-alive.
- Errors: 502 backend connect/reset, 503 no healthy backends, 504 backend timeout, 431 head over 16 KB, 501 chunked request body.
- Routed topology: 2 routers (FRR), 3 subnets, static routes, bridge on 10.0.2.0/24, netem scripts.
- Evidence: routing tables on both routers, traceroute c1 -> b1, pcapng for each key exchange with annotated screenshots.

Acceptance: unit tests pass; `active` returns to 0 after every request; 500 concurrent connections do not crash the server; `curl http://10.0.2.10:8080/` from c1 works; `set_rtt.sh 100` gives about 100 ms RTT.

## Sufyan
Files:
```
lb/interfaces.py (shared, frozen)   lb/config.py
lb/balancing/round_robin.py, least_conn.py, ip_hash.py, pool.py, health.py
docs/notes_balancing.md             tests/test_balancing.py
```
Delivers:
- `make_balancer`, `make_pool`, `make_health_checker`, `load_config`.
- Three algorithms as pure functions of (client_ip, healthy backends). No I/O.
- `pool.healthy()` returns config order. `acquire`/`release` track `active`.
- Health checker: GET /health every 1 s, timeout 0.5 s, DOWN after 3 consecutive failures, re-admit after 2 consecutive successes.
- Config: JSON canonical, YAML only if PyYAML installed, validation with clear errors.
- Unit tests for every algorithm, pool and health transition.

## Abdur Rehman
Files:
```
lb/obs/logger.py, metrics.py, stats_server.py, dashboard/
backends/app.py, Dockerfile, docker-compose.yml
haproxy/haproxy.cfg
bench/run_experiment.sh, collect.py, analyze.py, plots
docs/notes_obs.md                   tests/test_obs.py
```
Delivers:
- `make_logger` (JSON lines, event names per contract), `make_metrics` (p50/p95/p99, per-backend counts, health events), `start_stats_server` (JSON on port 9090) and a small dashboard.
- 3-4 backend containers, unequal capacity (5/10/25/50 ms, CPU limits 1.0/1.0/0.5/0.25), `GET /` and `GET /health`, configurable delay.
- HAProxy with equivalent settings: same port 8080, same health check, `retries 0`, no redispatch, algorithms roundrobin / leastconn / source.
- Benchmark scripts: 50/200/500 concurrency, 60 s, 5 s warm-up discarded, `docker kill` b3 at t=30 s, load split across c1-c4, restart between runs.
- Analysis: mean and std dev over 3 runs, pre-kill and post-kill separate, plots.

## Shared rules
- Interface `lb/interfaces.py` is frozen and tagged `lb-contract-v1`. No change without a PR all three approve.
- Branches and PRs only. No direct pushes to `main`. Branch names: `feat/lb-core`, `feat/lb-balancing`, `feat/lb-obs`.
- Each owner unit-tests their own module.
- `lb/` is Python standard library only. Third-party only in `bench/` and `backends/`.
- No globals or singletons. No hard-coded IPs or ports. Config or arguments only.
- Each module ships plain-English notes in `docs/notes_*.md`: what it does, why, one failure case.
- Scope stays inside the official Project 5 text. Optional extras only from the official list.
- Final summary documents stay short (about 6 pages), overview first.

## Handoffs
- Talha consumes Sufyan's balancer, pool and health checker, and Abdur's logger, metrics and stats server. Use fakes in tests until they land.
- Abdur's backends join the 10.0.3.0/24 network with default route via R2 (Talha's topology).
- Abdur's bench scripts call Talha's `set_rtt.sh` for Experiment 2.
- Checkpoint 1 (Talha): topology up, traceroute proof, one request client -> LB -> backend.
- Checkpoint 2 (all): one full 60 s run with the kill gives clean data.

## Phases
1. Contract: freeze and tag interface. Draft proposal.
2. Independent work per module.
3. Integration: merge, failover works, HAProxy baseline works, experiments scripted.
4. Experiments, captures, report, README, dashboard, live demo, GitHub.
