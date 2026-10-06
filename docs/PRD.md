# PRD: Software Load Balancer with Health Checks and Dynamic Traffic Distribution

Course: Computer Networks (CS-3001), Fall 2026, NUCES FAST Karachi. Instructor: Shoaib Raza.
Project: Project 5 on the official list.
Team: Talha, Sufyan, Abdur Rehman.

---

## 1. Problem
A service behind one server fails when that server fails or saturates. We build our own load balancer, compare balancing algorithms, and benchmark it against HAProxy.

**Learning objective:** how TCP connections and HTTP requests are distributed, and how health checking affects availability.

**Concepts covered:** TCP connection handling, HTTP persistent connections, server farms (data-center front ends), L4 vs L7 balancing. Course topics 2.2, 3.5, 6.6. CLOs 1, 2, 3*.

## 2. Scope (official Project 5 only)
Core POC:
- Python asyncio reverse proxy (L7, HTTP/1.1).
- Algorithms: round robin, least connections, IP-hash.
- Active health checks: HTTP GET `/health` every 1 s. Backend DOWN after 3 consecutive failures.
- 3-4 backend containers with unequal capacity, configurable artificial delay, health endpoint.
- Same setup repeated with HAProxy as the reference.
- Runs on a routed topology (not localhost).

Optional extras (pick 0-2, only from this list): dynamic weights from measured response time; consistent hashing; connection draining; TLS termination; L4 (TCP splice) vs L7 comparison.

Out of scope: connection pooling, caching, auth, filtering, rate limiting, anything not on the official list.

The report must explain L4 vs L7 balancing and server farms even if L4 is not built.

## 3. Course rules we must follow
- Group max 3. Zero purchases. One laptop must be enough.
- Every result is a measured metric backed by packet traces.
- Mandatory routed topology: at least 2 routers and 3 subnets. Built with Mininet, GNS3 + FRR/VyOS, containerlab + FRR, or Linux network namespaces. Static or OSPF routing. Show routing tables and one traceroute. Impairments via `tc netem` / `tc tbf`.
- Lab: Ubuntu 22.04/24.04 (native, VM or WSL2). Tools: Wireshark, tcpdump, iperf3, ping, traceroute, ss, ip, tc, curl, Docker, Python 3 + Scapy, HAProxy, Nginx, wrk, hey.
- AI-use disclosure required. Written manually by the group as one general statement.

## 4. Deliverables
1. Proposal, items a-i: title; problem and objectives; network architecture; protocols and technologies; addressing plan; proposed POC; methodology and metrics; expected outcomes; team and responsibilities.
2. Architecture diagram with addressing plan: subnets, IPs, ports, devices, routers/switches, protocol flows.
3. Working POC, demonstrated live.
4. Packet captures: at least one `.pcapng` per key protocol exchange, plus annotated screenshots.
5. Experimental evaluation: each key metric under at least 3 network conditions, at least 3 runs per condition, mean and spread.
6. Technical report (6-10 pages) plus README so another group can reproduce setup, POC and experiments.
7. AI-use disclosure.

Full tick-list: `docs/requirements-checklist.md` (if added).

## 5. Locked design decisions
- D1. L7 (HTTP-aware) reverse proxy is the core. L4 TCP relay is an optional comparison only.
- D2. Client-to-LB: HTTP/1.1 keep-alive supported. LB-to-backend: new TCP connection per request. No connection pooling.
- D3. Balancer is a pure function of (client_ip, healthy backends). No I/O inside algorithms.
- D4. Health: GET /health every 1 s. DOWN after 3 consecutive failures. Re-admit after 2 consecutive successes. Probe timeout 0.5 s.
- D5. Static routes (no OSPF) unless time remains.
- D6. Experiment 1 = load levels (50/200/500 concurrency). Experiment 2 = network conditions (RTT 20/100/200 ms via netem). Experiment 2 is mandatory and reports ALL key metrics. Optional: loss 0/2/5%.
- D7. Each 60 s run includes the backend kill at t=30 s. Report pre-kill (0-30 s) and post-kill (30-60 s) separately.
- D8. Logs are JSON lines. Stats exposed as JSON on a stats port. Dashboard reads that JSON.
- D9. Core stays standard-library only (asyncio, json, logging, hashlib, statistics). Third-party only in `bench/` and `backends/`.
- D10. Kill target: b3 at t=30 s using `docker kill`. Same for HAProxy runs. Restart before the next run.
- D11. wrk cannot bind a source IP. Split total concurrency evenly across c1-c4, one load process per client host, so IP-hash sees four client IPs.
- D12. Config: JSON canonical. YAML accepted only if PyYAML is installed.
- D13. `pool.healthy()` returns backends in config order.
- D14. Dashboard is a small stats view for the demo.

## 6. Architecture and addressing

```
 clients                R1                 LB subnet               R2               backends
10.0.1.0/24 --[.1]--- R1 ---[.1]  10.0.2.0/24  [.2]--- R2 ---[.1]--- 10.0.3.0/24
 c1 .11                          LB      .10                          b1 .11
 c2 .12                          HAProxy .11 (one active at a time)   b2 .12
 c3 .13                                                               b3 .13
 c4 .14                                                               b4 .14
```

| Subnet | Purpose | Router IPs | Hosts |
|---|---|---|---|
| 10.0.1.0/24 | Clients | R1 eth1 = 10.0.1.1 | c1-c4 = 10.0.1.11-.14 |
| 10.0.2.0/24 | LB / transit | R1 eth2 = 10.0.2.1, R2 eth1 = 10.0.2.2 | Own LB = 10.0.2.10, HAProxy = 10.0.2.11 |
| 10.0.3.0/24 | Backends | R2 eth2 = 10.0.3.1 | b1-b4 = 10.0.3.11-.14 |

10.0.2.0/24 is a shared segment with four attachments (R1, R2, LB, HAProxy). Use a Linux bridge for it and show it in the diagram.

Routes:
- R1: 10.0.3.0/24 via 10.0.2.2.
- R2: 10.0.1.0/24 via 10.0.2.1.
- Clients: default via 10.0.1.1. Backends: default via 10.0.3.1.
- LB/HAProxy host: 10.0.1.0/24 via 10.0.2.1; 10.0.3.0/24 via 10.0.2.2.
- Proof: traceroute c1 -> b1 crosses 10.0.1.1, 10.0.2.2, 10.0.3.11.

Ports:
| Service | Port |
|---|---|
| Own LB (clients connect here) | 8080/tcp |
| HAProxy (same port, different IP) | 8080/tcp |
| HAProxy stats | 8404/tcp |
| Backend app | 8000/tcp (GET /, GET /health) |
| LB stats JSON + dashboard | 9090/tcp |

Impairments: `tc netem` delay and loss on R1 eth1 and R1 eth2 egress so the client-to-LB path hits the target RTT. Set both directions.

Backends (unequal capacity, adjustable, freeze before experiments):
| Backend | Artificial delay | Docker CPU limit |
|---|---|---|
| b1 | 5 ms | 1.0 |
| b2 | 10 ms | 1.0 |
| b3 | 25 ms | 0.5 |
| b4 | 50 ms | 0.25 |

Tooling: containerlab (FRR routers + Docker hosts) on Linux/WSL2. Fallback: Linux network namespaces + Docker backends.

## 7. Frozen interface contract (`lb/interfaces.py`, tag `lb-contract-v1`)

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, Sequence, Optional, Mapping, Any

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
    def healthy(self) -> list[Backend]: ...
    def all(self) -> list[Backend]: ...
    def acquire(self, b: Backend) -> None: ...      # active += 1
    def release(self, b: Backend) -> None: ...      # active -= 1
    def set_health(self, backend_id: str, healthy: bool, reason: str) -> None: ...

class HealthChecker(Protocol):
    async def run(self) -> None: ...   # loops until stop(); calls pool.set_health
    def stop(self) -> None: ...

class EventLogger(Protocol):
    def event(self, name: str, **fields: Any) -> None: ...   # one JSON line, logger adds ts

class Metrics(Protocol):
    def request_done(self, backend_id: Optional[str], status: int, latency_ms: float,
                     bytes_up: int, bytes_down: int, client_ip: str) -> None: ...
    def health_transition(self, backend_id: str, healthy: bool, reason: str) -> None: ...
    def snapshot(self) -> Mapping[str, Any]: ...
```

Factories (each owner implements theirs):
- `make_balancer(name: str) -> Balancer` (Sufyan)
- `make_pool(cfg, log, metrics) -> BackendPool` (Sufyan)
- `make_health_checker(cfg, pool, log) -> HealthChecker` (Sufyan)
- `load_config(path) -> dict` (Sufyan)
- `make_logger(cfg) -> EventLogger` (Abdur)
- `make_metrics(cfg) -> Metrics` (Abdur)
- `async start_stats_server(cfg, metrics, pool) -> AbstractServer` (Abdur)
- `async serve(cfg, balancer, pool, log, metrics) -> None` (Talha)

Config (JSON canonical):
```yaml
listen: {host: 0.0.0.0, port: 8080}
algorithm: round_robin          # round_robin | least_conn | ip_hash
backends:
  - {id: b1, host: 10.0.3.11, port: 8000}
health: {path: /health, interval_s: 1.0, timeout_s: 0.5, fail_threshold: 3, success_threshold: 2}
timeouts: {connect_s: 2.0, read_s: 10.0, keepalive_s: 15.0}
logging: {path: logs/lb.jsonl, level: INFO}
stats: {host: 0.0.0.0, port: 9090}
```

Log event names: `request`, `backend_selected`, `backend_error`, `backend_down`, `backend_up`, `health_probe`, `client_error`, `startup`, `shutdown`.

Stats snapshot schema:
```json
{"uptime_s": 0, "algorithm": "round_robin", "total_requests": 0, "errors": 0,
 "latency_ms": {"p50": 0, "p95": 0, "p99": 0},
 "per_backend": {"b1": {"requests": 0, "errors": 0, "active": 0, "healthy": true, "bytes_up": 0, "bytes_down": 0}},
 "health_events": [{"ts": "", "backend": "b1", "healthy": false, "reason": ""}]}
```

## 8. Repo layout
```
lb/
  interfaces.py          frozen contract (tag lb-contract-v1)
  config.py              loader + validation (Sufyan)
  core/                  Talha: server.py, http_io.py, forward.py
  balancing/             Sufyan: round_robin.py, least_conn.py, ip_hash.py, pool.py, health.py
  obs/                   Abdur: logger.py, metrics.py, stats_server.py, dashboard/
backends/                Abdur: app.py, Dockerfile, docker-compose.yml
topo/                    Talha: containerlab/netns scripts, routes, netem scripts
haproxy/                 Abdur: haproxy.cfg equivalent settings
bench/                   Abdur: run_experiment.sh, collect.py, analyze.py, plots
captures/                pcapng files
docs/                    proposal, report, README, AI-use disclosure
tests/                   each owner tests own module
```

## 9. Experiment plan
**Experiment 1 (algorithm x concurrency):** own LB and HAProxy. Algorithms: round robin, least conn, IP-hash (HAProxy: roundrobin, leastconn, source). Concurrency 50, 200, 500. 60 s. b3 killed at t=30 s. 3 runs each. 3 x 3 x 3 x 2 = 54 runs.

**Experiment 2 (network condition, mandatory):** concurrency 200, round robin. RTT 20/100/200 ms via netem. 3 runs each. Both systems. 3 x 3 x 2 = 18 runs.

Total about 72 runs of 60 s plus reset. Script everything. Restart the killed backend between runs.

Metrics and how measured:
- Requests/s, p50/p95/p99: wrk/hey output. Mean of per-run values plus std dev. Do not average raw percentiles across mixed data.
- Requests per backend: LB stats snapshot at run end.
- Failure detection time: kill timestamp (script) to `backend_down` log timestamp.
- Failed requests during failure: non-2xx plus connection errors in the post-kill window.
- LB CPU: psutil or pidstat sampled each second; mean and peak.
- Pre-kill and post-kill reported separately.

Pitfalls:
- Load generator, LB and backends share one laptop. Pin CPUs (taskset / docker --cpus) and record limits.
- 500 connections may hit file-descriptor limits: `ulimit -n 65535`.
- netem applies egress only. Set both directions.
- Python asyncio LB may be slower than HAProxy. That is a finding, not a failure. Explain it.
- HAProxy retries and redispatches failed connections by default. Set `retries 0` and no redispatch to match our no-retry LB.
- Detection time is about 2-3.5 s (3 failed probes 1 s apart, plus up to 0.5 s timeout). In that window about 1 in 4 requests go to the dead backend and fail under round robin. That is the "failed requests during failure" number.
- Warm up 5 s and discard. Record laptop specs, CPU limits, kernel and tool versions in the README.

Packet captures (.pcapng each):
1. Client to LB: TCP handshake + HTTP request/response, keep-alive reuse.
2. LB to backend: forwarded request with X-Forwarded-For.
3. Health probe: GET /health, 200 and failure cases.
4. Failover: backend killed, FIN/RST/refused, probe failures, backend removed.
5. HAProxy equivalent of 1 and 4.

## 10. Phases and checkpoints
- Phase 1 (contract): write and tag `lb/interfaces.py`. Everyone approves. Draft proposal.
- Phase 2 (independent): Talha core + topology; Sufyan algorithms/health; Abdur backends/logging/metrics/bench scripts.
- Checkpoint 1: topology up with traceroute proof. One request passes client -> LB -> backend.
- Phase 3 (integration): all modules merged; health failover works; HAProxy baseline works; experiments scripted.
- Checkpoint 2: one full 60 s run with kill produces clean data.
- Phase 4: all experiments, captures, report, README, dashboard UI, live demo, GitHub.

## 11. Open decisions
1. Confirm containerlab host: WSL2 or VM (Talha is on Windows).
2. Retry on connect failure to next backend? Default OFF (keeps detection-time metric clean). Decide before experiments.
3. Which 1-2 optional extras, if any (suggest: connection draining, or L4 vs L7).
4. Freeze backend delay/CPU values.
5. Confirm JSON as canonical config format with Sufyan.
6. Instructor confirmations: exact proposal due date; meaning of CLO "3*"; AI-use policy text; whether Experiment 1 load levels are accepted alongside Experiment 2.
