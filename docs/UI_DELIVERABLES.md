# UI deliverables: live, annotated presentation of the project

Purpose: a browser page, part of the project, that presents the system by querying the RUNNING topology and annotating what it finds.
It is the Phase 4 "dashboard UI + live demo" deliverable (docs/PRD.md section 10). Status of every source below is as of `main` after PR #6.

## Rules for the UI
1. Live only. No old images, screenshots, report text, README, proposal or AI-use disclosure on screen.
2. Every panel shows a timestamp and its source. If a source is offline it says "no data". Never show sample or invented values.
3. Anything read from `results/` is labelled with its experiment. `results/checkpoint2/` is a dry run and must be labelled "dry run, not report data".
4. Scope: official Project 5 only (CLAUDE.md). Agree the page with Abdur, who owns `lb/obs/dashboard/`.
5. Constraint: a Windows browser cannot reach the container IPs (10.0.x.x) inside WSL2. A small relay running in WSL runs a fixed allowlist
   of commands and serves JSON to the browser on localhost. The relay accepts no user-supplied arguments (command injection) and binds to localhost.
   Port forwarding from WSL to Windows is not yet tested.

## 1. Deliverables
"Ready" means the data source works today. It does not mean the panel exists.

| ID | Panel | Shows (live) | Annotation on screen | Source | Ready |
|---|---|---|---|---|---|
| U1 | Live topology map | c1-c4, R1, R2, 3 bridges, own LB, HAProxy, b1-b4, every IP and port, up/down colour | Subnets, the 10.0.2.0/24 bridge, flow arrows (request, forward, probe, stats) | `docker ps`, `docker exec <node> ip -br addr`, links from `topo/clab.yml` | Yes |
| U2 | Routing proof | `ip route` on R1, R2, LB, c1, b1 and a live traceroute c1 to b1 | 10.0.1.1 = R1, 10.0.2.2 = R2, 10.0.3.11 = b1 | `docker exec` (same commands as `topo/verify.sh`) | Yes |
| U3 | Network condition | Current netem delay and loss on R1, measured ping RTT and loss | "configured 100 ms, measured 100.4 ms" | `tc qdisc show` on R1, `topo/.netem_state`, `ping` from c1 | Yes |
| U4 | Single request walkthrough | One live `curl -i` shown as 3 hops with headers | `X-Backend-Id`, `X-Forwarded-For`, `Connection: close` (LB to backend), keep-alive (client to LB) | `docker exec c1 curl`, `topo/capture.sh` + `tshark` | Yes |
| U5 | Live annotated packets | Short on-demand capture on the LB port, decoded and labelled, `.pcapng` download | Handshake, 1 SYN for N requests, probe every 1 s, RST on refused | `topo/capture.sh`, `tshark -T fields` (installed in WSL) | Yes |
| U6 | Own-LB metrics | Requests/s (from counter deltas), p50/p95/p99, errors, per-backend requests, active, uptime | Which backend is slow and why (5/10/25/50 ms) | `GET http://10.0.2.10:9090/stats` | Yes |
| U7 | Algorithm view | Current algorithm and request distribution per backend | Round robin is even, least-conn favours fast backends, IP-hash sticks per client | `/stats` fields `algorithm`, `per_backend` | Yes |
| U8 | Health and failover timeline | Backend states, `backend_down` / `backend_up` events with reason, kill time, detection time, failed requests in the window | "refused, 3 probes, about 3 s" | `logs/lb.jsonl`, `kill_ts` from `backends/kill_backend.sh`, `/stats` `health_events` | Yes |
| U9 | HAProxy side-by-side | Requests per server, UP/DOWN, detection time from its log | Own LB vs reference | `http://10.0.2.11:8404/;csv`, `docker logs -t clab-lb-haproxy` | Partial: CSV has averages only, no p50/p95/p99 |
| U10 | Backend capacity | b1-b4 delay and CPU limit, live `/health` timing | "Unequal capacity by design" | `backends/.env`, `curl http://10.0.3.1x:8000/health` | Yes |
| U11 | Experiment results | rps vs concurrency, p95, per-backend requests, detection time, CPU; mean and std, pre/post kill separate, valid runs only, n | Own LB vs HAProxy, CPU-bound finding | `results/*/*/summary.json`, `results/tables/*.csv`, drawn as live charts | Blocked: the full 72 runs do not exist, only the dry run |
| U12 | Run integrity and environment | Machine and tool versions, real CPU limits, seed, validity reasons | Flags invalid runs and why | `results/*/environment.txt`, `summary.json` `valid` / `invalid_reason`, `docker inspect` `CpuQuota` | Partial: `bench/environment.sh` prints "unlimited" wrongly |
| U13 | Guided demo script | Steps: start load, kill b3, watch detection, restore, change RTT | Short captions on L4 vs L7, keep-alive, server farms | Static text plus the panels above | Content to write |
| U14 | Freshness and status | Last update per panel, stale/offline banner, dry-run vs real label | | Panel timestamps | Yes |

Optional controls (decide with the group): kill / restore b3, set RTT, start / stop load, switch algorithm. Fixed actions only, see section 3.

## 2. File-wise breakdown: which project file feeds the UI
| File or folder | What the UI takes from it | Panels |
|---|---|---|
| `lb/obs/stats_server.py` | Serves `GET /stats` (JSON), `/healthz`, and `/` (the dashboard page) on port 9090 | U6, U7, U8 |
| `lb/obs/dashboard/index.html` | Existing dashboard that polls `/stats` every second. Reuse or link it, do not duplicate | U6 |
| `lb/obs/metrics.py` | Defines the `/stats` fields (`uptime_s`, `total_requests`, `errors`, `latency_ms`, `per_backend`, `health_events`) | U6, U7, U8 |
| `lb/obs/logger.py` | Defines the `logs/lb.jsonl` format. Events: `startup`, `shutdown`, `request`, `backend_error`, `client_error`, `backend_down`, `backend_up`. `health_probe` is DEBUG only and not written at INFO | U8 |
| `lb/config.py`, `config.example.json` | Algorithm, backends, ports, health and timeout settings to annotate | U1, U7 |
| `lb/balancing/*`, `lb/core/*` | Source of the behaviour being shown. Not read by the UI at run time | U7, U13 |
| `topo/clab.yml` | Node names, links, IPs. Parse to build the map | U1 |
| `topo/env.sh` | Container prefix `clab-lb-`, IPs, ports, node lists. Source it, do not copy values | all topology panels |
| `topo/verify.sh`, `topo/routes.sh` | The exact commands for routing tables, traceroute, ping and curl | U2, U4 |
| `topo/set_rtt.sh`, `topo/set_loss.sh`, `topo/.netem_state` | Impairment control and the current configured value | U3 |
| `topo/capture.sh` | Runs tcpdump in a throw-away container, writes `captures/<name>.pcap` | U5 |
| `captures/README.md` | The filter and the expected pattern for each exchange. Source for the annotation rules | U5, U4 |
| `backends/app.py`, `backends/.env` | `GET /` and `GET /health`; delay and CPU table | U10 |
| `backends/kill_backend.sh`, `start_backend.sh` | Kill and restore b3; `KILL_TS_FILE` gives the kill time in ms | U8, U13 |
| `haproxy/haproxy.cfg.tmpl`, `render.sh` | Reference system settings; HAProxy stats at 8404 | U9 |
| `bench/run_experiment.sh`, `run_all.sh` | How one run and the full plan are produced and where files land | U11, U12 |
| `bench/collect.py` | Defines `summary.json` | U11, U12 |
| `bench/analyze.py` | Writes `results/tables/<exp>.csv`; the UI draws its own charts from these, not the PNGs | U11 |
| `bench/cpu_sampler.py`, `bench/environment.sh` | `cpu.csv` per run; environment text (CPU-limit lines are wrong, read `CpuQuota` instead) | U12 |
| `docs/evidence/docker_kill_behaviour.md`, `checkpoint2.md` | Expected values to annotate against (refused, about 3 s detection). Text only, not displayed as images | U8, U13 |
| `docs/architecture.dot` | Reference for what U1 must show. The static PNG is not displayed | U1 |

## 3. Command allowlist for the relay (fixed, no arguments from the browser)
- Read-only: `docker ps`, `docker exec clab-lb-<node> ip -br addr`, `ip route`, `tc qdisc show`, `docker exec clab-lb-c1 ping -c 3 10.0.2.10`, `traceroute -n 10.0.3.11`, `curl -s <stats/health url>`, `docker logs -t clab-lb-haproxy`, reading `logs/lb.jsonl` and `results/**`.
- Capture: `topo/capture.sh` with fixed names and a fixed filter, then `tshark -r <file> -T fields ...`.
- Controls (only if approved): `backends/kill_backend.sh b3`, `backends/start_backend.sh b3`, `topo/set_rtt.sh <value from a fixed list: 0, 20, 100, 200>`, `hey` on c1-c4 with a fixed concurrency list (50, 200, 500) and duration.
- Node names come from a fixed list: c1-c4, r1, r2, lb, haproxy, b1-b4.

## 4. Data formats
- `/stats`: `{uptime_s, algorithm, total_requests, errors, latency_ms{p50,p95,p99}, per_backend{id:{requests,errors,active,healthy,bytes_up,bytes_down}}, health_events[{ts,backend,healthy,reason}]}`.
- `lb.jsonl`: one JSON object per line, `ts` is UTC ISO with milliseconds and `Z`, then `event` and fields. `backend_down` has `reason` such as `3 consecutive failures: refused`.
- `summary.json`: `exp, system, algorithm, concurrency, rtt_ms, run, t0_ms, kill_ms, down_ms, detection_s, windows{pre,post:{rps,p50_ms,p95_ms,p99_ms,requests,failed}}, failed_in_detection_window, per_backend_pre, per_backend_post, cpu_pct{pre_mean,post_mean,peak}, valid, invalid_reason`.
- HAProxy CSV: header starts `# pxname,svname,...`. Read columns by name (`status`, `stot`, `rtime`, `ttime`), not by position.
- Detection time = first `backend_down` for b3 after `kill_ts` minus `kill_ts`. Ignore a `backend_down` that happened before the kill (startup flap).

## 5. Proposed UI files (suggestion, not decided)
| File | Role |
|---|---|
| `ui/relay.py` | Stdlib Python server in WSL: the allowlist above, serves `/api/*` JSON and the static page, binds to localhost |
| `ui/static/index.html` | Single page, no build step |
| `ui/static/panels/*.js` | One module per panel U1-U14 |
| `ui/annotations.json` | Annotation text per panel and per packet pattern, kept apart from code |
| `ui/README.md` | How to start the lab, the relay and the page |

Open questions for the group: read-only or controls; HAProxy side-by-side live (needs switching which system gets load); folder and branch (`ui/`, `feat/ui`);
demo from one laptop with a Windows browser; whether the 72 runs finish before the demo (otherwise U11 stays "dry run").

## 6. Not shown in the UI
The static `architecture.png/.svg`, screenshots, saved captures as images, the report, README, proposal PDF, AI-use disclosure.
