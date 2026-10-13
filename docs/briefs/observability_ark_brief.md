# BRIEF FOR ABDUR'S AI (v2, expanded): Logging, Metrics, Stats, Dashboard, Backends, HAProxy, Benchmarks
Version v2, 2026-10-09. Replaces v1. Same scope, much more detail. Changes from v1 are listed in section 0.

## 0. How to use this brief
- Read sections 1-4 first. They explain what you are building and where it sits.
- Build in the order in section 16.
- Section 19 (Rules) and section 20 (Not to build) are mandatory.
- If something is unclear, write the question in your report-back. Do not guess silently.

**What changed from v1:**
1. Load tool is `hey` with CSV output (per-request time offsets), so pre-kill and post-kill windows can be computed. `wrk` is the fallback.
2. Client hosts c1-c4 need a client image with `hey` installed (new: `bench/client/Dockerfile`).
3. Full HAProxy config template, with a mapping table and a rule for matching our "new backend connection per request" design.
4. Exact run timeline, output folder layout and `summary.json` schema, so `collect.py` and `analyze.py` have a fixed input.
5. Stats snapshots at start, at the kill moment (t=30 s) and at end. This gives per-backend counts before and after the kill.
6. LB CPU is read from the container's cgroup, not `top`.
7. Both LB and HAProxy get the same CPU limit (fairness rule).
8. Backends ship as one Docker image used by both `docker-compose.yml` (dev) and the containerlab topology (final).
9. Section 12: behaviours that must be documented, not fixed (including what `docker kill` really does on the network).
10. Validity checks for every run.

## 1. Context
Group project for a Computer Networks course (CS-3001, Project 5: Software Load Balancer with Health Checks and Dynamic Traffic Distribution). We build a Python asyncio HTTP/1.1 **load balancer** (L7 reverse proxy) that spreads client requests across 3-4 backend servers of unequal capacity. It has three algorithms (round robin, least connections, IP-hash) and active health checks (GET /health every 1 s, DOWN after 3 failures, UP after 2 successes). It is benchmarked against **HAProxy** on a routed topology (2 routers, 3 subnets). Every result must be a measured number backed by packet traces.

Three people work in parallel behind a **frozen interface** (`lb/interfaces.py`, tag `lb-contract-v1`). You own **observability** (logger, metrics, stats server, dashboard) plus **backend servers, HAProxy config and the benchmark tooling**.

Your work produces the numbers the instructor grades. Deliverable 5 (experimental evaluation) is yours. A wrong timestamp or a wrong percentile becomes a wrong result in the report. Be precise.

## 2. Primer (only what you need)
- **Load balancer.** Clients connect to the LB. It picks one backend per request and relays the response.
- **Closed-loop load.** `hey -c N` runs N workers. Each sends a request, waits for the response, sends the next. Throughput is limited by latency, not by a fixed rate. If the system slows down, the offered load slows down too.
- **Percentiles (p50/p95/p99).** p95 = 95% of requests were faster than this value. Averages hide slow requests. Never average percentiles from different runs. Compute the percentile per run, then take the mean and standard deviation of those per-run values.
- **Requests per backend (load distribution).** Shows whether the algorithm spreads load as designed. Round robin is even by count. Least connections should favour fast backends.
- **Failure detection time.** `backend_down` log timestamp minus the kill timestamp. Expect about 2 to 3.5 s with a 1 s probe interval and 3 failures.
- **Failed requests during failure.** Requests that returned an error (502/503/504 or no response) after the kill. Under round robin with no retry, about 1 in 4 requests go to the dead backend until it is marked DOWN.
- **Why pre-kill and post-kill are separate.** Before the kill we measure normal behaviour. After the kill we measure degraded behaviour with 3 backends. Mixing them hides both.
- **Why HAProxy is the baseline.** It is a mature C implementation. The gap between it and our Python LB is a finding. We measure and explain it. We do not hide it.
- **Unequal capacity.** Backends have delays 5 / 10 / 25 / 50 ms and CPU limits 1.0 / 1.0 / 0.5 / 0.25. Round robin ignores this. Least connections adapts.

## 3. Where your code sits
```
clients c1-c4 (hey) --> [LB 10.0.2.10:8080 or HAProxy 10.0.2.11:8080] --> backends b1-b4 (10.0.3.11-.14:8000)

Inside our LB (one process, one asyncio loop):
  Talha's serve() --> log.event("request", ...)        --> [your Logger]
                  --> metrics.request_done(...)        --> [your Metrics]
  Sufyan's pool.set_health() --> log.event("backend_down"/"backend_up") + metrics.health_transition()
  [your stats server] reads metrics.snapshot() + pool.all()  --> GET :9090/stats  --> dashboard
```
- Everything in `lb/obs/` runs on the LB's event loop. No locks needed. Never block it.
- `request_done` and `event` are called on every request. Keep them O(1) and cheap.

## 4. Your files
```
lb/obs/__init__.py            # exports exactly: make_logger, make_metrics, start_stats_server
lb/obs/logger.py
lb/obs/metrics.py
lb/obs/stats_server.py
lb/obs/dashboard/index.html
backends/app.py
backends/Dockerfile
backends/docker-compose.yml
backends/kill_backend.sh
backends/start_backend.sh
haproxy/haproxy.cfg.tmpl       # envsubst template, variable ${ALGO}
haproxy/render.sh              # renders haproxy.cfg for roundrobin|leastconn|source
bench/client/Dockerfile        # client image with hey + wrk + curl
bench/make_configs.py          # writes config.<algo>.json for our LB from one template
bench/run_experiment.sh
bench/run_all.sh               # runs the whole plan in randomised order
bench/collect.py
bench/analyze.py
bench/environment.sh           # records laptop and tool versions
tests/test_obs.py
tests/test_collect.py
docs/notes_observability.md
```

## 5. Frozen interface (conform to it)
```python
from __future__ import annotations
from typing import Protocol, Optional, Mapping, Any

class EventLogger(Protocol):
    def event(self, name: str, **fields: Any) -> None: ...   # ONE JSON line; you add "ts"

class Metrics(Protocol):
    def request_done(self, backend_id: Optional[str], status: int, latency_ms: float,
                     bytes_up: int, bytes_down: int, client_ip: str) -> None: ...
    def health_transition(self, backend_id: str, healthy: bool, reason: str) -> None: ...
    def snapshot(self) -> Mapping[str, Any]: ...
```
Factories you implement:
- `make_logger(cfg: Mapping) -> EventLogger`
- `make_metrics(cfg: Mapping) -> Metrics`
- `async start_stats_server(cfg: Mapping, metrics: Metrics, pool) -> asyncio.AbstractServer` (`pool.all()` returns backends with `.id`, `.healthy`, `.active`)

Config you consume (already validated by Sufyan's `load_config`; plain dict):
```
logging: {path: logs/lb.jsonl, level: INFO}
stats:   {host: 0.0.0.0, port: 9090}
algorithm: round_robin | least_conn | ip_hash
```

## 6. Part A: Logger (`logger.py`)
**Behaviour**
- One JSON object per line. Fields: `ts` (UTC ISO-8601 with milliseconds and `Z`, for example `2026-10-09T14:03:21.482Z`), `event`, then the caller's fields.
- `ts` must be taken when `event()` is called, not when the line is flushed.
- Never raise from `event()`. If a value is not JSON-serialisable, convert it with `str()`. If the write fails, drop the line and count it in an internal `dropped` counter.
- Never block the loop. Use a buffered file object and flush every 1 s from a loop callback, plus on shutdown. Provide `close()` on the returned object (extra method is fine; the Protocol is the minimum).
- Create the log directory if missing. Open the file in append mode.
- Do not log request bodies or full headers.

**Levels** (from `logging.level`; order DEBUG < INFO < WARNING < ERROR):
| Event | Level | Fields (minimum) |
|---|---|---|
| `startup` | INFO | `algorithm`, `listen`, `backends` |
| `shutdown` | INFO | `reason` |
| `request` | INFO | `client`, `method`, `path`, `backend`, `status`, `latency_ms`, `bytes_up`, `bytes_down` |
| `backend_selected` | DEBUG | `client`, `backend`, `algorithm` |
| `backend_error` | WARNING | `backend`, `error` |
| `client_error` | INFO | `client`, `error` |
| `health_probe` | DEBUG | `backend`, `ok`, `status`, `rtt_ms`, `error` |
| `backend_down` | always | `backend`, `reason` |
| `backend_up` | always | `backend`, `reason` |

`backend_down` and `backend_up` are always written, whatever the level. The detection-time metric depends on them.

**Performance note.** Logging every `request` at 3000+ requests/s costs CPU and distorts the LB CPU metric for both systems unevenly (HAProxy logs to stdout). Do not add config keys. For benchmark runs set `logging.level: WARNING` (the always-on `backend_down`/`backend_up` events are still written). For demos and captures use `INFO`. State this in `docs/notes_observability.md` and in the README. Failed-request counts then come from the load tool output and the stats snapshots, not from the LB request log.

## 7. Part B: Metrics (`metrics.py`)
**State (all bounded):**
- `total_requests`, `errors` (status >= 500 **or** `backend_id is None`).
- Per backend: `requests`, `errors`, `bytes_up`, `bytes_down`. Created on first sight.
- Latency samples: `collections.deque(maxlen=100000)` of floats. Document the bound: after 100,000 requests the percentiles describe the most recent 100,000.
- `health_events`: `deque(maxlen=100)` of `{"ts", "backend", "healthy", "reason"}`.
- `start = time.monotonic()` for `uptime_s`.

**`request_done`:** O(1). Increment counters, append the latency. No sorting here.

**Percentiles** are computed only in `snapshot()` (nearest-rank method):
```python
def percentile(sorted_vals, p):          # p in (0, 100]
    if not sorted_vals: return 0.0
    k = max(1, math.ceil(p / 100 * len(sorted_vals)))
    return sorted_vals[k - 1]
```
`snapshot()` sorts a copy of the deque. At 100,000 samples that takes a few tens of ms. The dashboard polls once per second, which is fine. Cache the sorted copy for 1 s so repeated polls do not re-sort.

**Snapshot schema (exact keys, no extras):**
```json
{"uptime_s": 0, "algorithm": "round_robin", "total_requests": 0, "errors": 0,
 "latency_ms": {"p50": 0, "p95": 0, "p99": 0},
 "per_backend": {"b1": {"requests": 0, "errors": 0, "active": 0, "healthy": true, "bytes_up": 0, "bytes_down": 0}},
 "health_events": [{"ts": "", "backend": "b1", "healthy": false, "reason": ""}]}
```
- `metrics.snapshot()` alone does not know `active`, `healthy` or `algorithm`. The **stats server** merges them: `algorithm` from cfg, `active` and `healthy` from `pool.all()`. Backends known to the pool but never used still appear with zeros.
- Floats rounded to 3 decimals.

## 8. Part C: Stats server and dashboard (`stats_server.py`, `dashboard/index.html`)
**Server:** pure asyncio (`asyncio.start_server`), no framework.
| Route | Response |
|---|---|
| `GET /stats` | `200`, `Content-Type: application/json`, `Cache-Control: no-store`, the merged snapshot |
| `GET /` | `200`, `text/html`, contents of `dashboard/index.html` (read once at start) |
| `GET /healthz` | `200 ok` |
| anything else | `404`; non-GET gives `405` |
- Parse only the request line and drain headers up to `\r\n\r\n` (limit 8 KB). `Connection: close` on every response. One short coroutine per connection. Never raise out of the handler.
- Must keep working while the LB is under 500 concurrent connections. Stats handling must not take noticeable time. Do not do per-request work here.

**Dashboard:** one static HTML file, plain JS, no libraries, no CDN, no external fonts. Polls `/stats` every 1 s with `fetch`. Shows:
- algorithm, uptime, total requests, errors, error %,
- p50 / p95 / p99,
- table: backend, requests, share % (of total), active, healthy (green dot or red dot **and** the words UP or DOWN, not colour alone), errors,
- recent health events (newest first, last 10),
- a "connection lost" banner if three polls in a row fail.
Large font, high contrast. Readable on a projector from the back of a lab. Keep it small (Decision D14: it supports the required metrics, nothing more).

## 9. Part D: Backends (`backends/`)
**`app.py`:** HTTP/1.1 server, `asyncio` streams only (so there are no extra dependencies and the delay is non-blocking). Requirements:
- Env: `BACKEND_ID` (required), `DELAY_MS` (default 0), `PORT` (default 8000).
- `GET /` waits `DELAY_MS` using `await asyncio.sleep(DELAY_MS / 1000)`, then returns `200` with a small body, for example `{"backend":"b3","ts":"...","delay_ms":25}`, `Content-Type: application/json`, correct `Content-Length`. Add the header `X-Backend-Id: b3` (useful in captures).
- `GET /health` returns `200`, body `ok`, **no delay**.
- Supports HTTP/1.1 keep-alive (loop on the connection until `Connection: close` or EOF). Our LB uses `Connection: close` to backends in v1, but keep-alive must still work, because HAProxy may reuse connections.
- Unknown path: `404`. Malformed request: `400`. Never crash on a bad client.
- Log one line per start (`backend b3 listening on :8000 delay=25ms`). Do **not** log per request (it distorts the CPU-limited backends).

**`Dockerfile`:** `python:3.11-slim`, copy `app.py`, `CMD ["python","app.py"]`, image name `cn-lb-backend:latest`. No pip packages.

**Compose vs containerlab (important):**
- The same image is used in two places. `docker-compose.yml` is for **development** (Phase 2, before Talha's topology exists). Talha's containerlab file (`topo/clab.yml`) uses the **same image** and the same env vars for the final topology.
- Compose: four services `b1`..`b4`, `DELAY_MS` 5 / 10 / 25 / 50, `cpus` 1.0 / 1.0 / 0.5 / 0.25, container port 8000. Put delays and CPU limits in a `.env` file at the top, so they are edited in one place. Give Talha the table of values (he copies them into `clab.yml`).
- Expose a fixed host port per backend in compose for dev only (`8001`..`8004`).

**Helper scripts:**
- `kill_backend.sh <id>`: runs `date -u +%s%3N` **immediately before** `docker kill`, writes the number to `$KILL_TS_FILE` (default `./kill_ts.txt`), then runs `docker kill <container>`. Container name = `${BACKEND_PREFIX:-}${id}` (containerlab names are `clab-<lab>-b3`, so `BACKEND_PREFIX` must be configurable).
- `start_backend.sh <id>`: `docker start`, then waits until `/health` answers 200 from the host or from a client container, then prints the UTC ms timestamp.
- Both print the timestamp so the run script can log it.

## 10. Part E: HAProxy reference (`haproxy/`)
Pin the version: use the official image `haproxy:2.8`. Check config with `haproxy -c -f ...` in the render script.

**Template (`haproxy.cfg.tmpl`)**, `${ALGO}` is `roundrobin`, `leastconn` or `source`:
```
global
    log stdout format raw local0 info
    maxconn 20000

defaults
    mode http
    log global
    option httplog
    option forwardfor
    option http-server-close      # client keep-alive, new server connection per request (matches our core, decision D2)
    retries 0                     # no retry (matches our LB)
    # no "option redispatch"      # do not move a failed request to another server
    timeout connect 2s            # = timeouts.connect_s
    timeout server  10s           # = timeouts.read_s
    timeout http-keep-alive 15s   # = timeouts.keepalive_s
    timeout client  30s
    timeout http-request 10s
    timeout check   500ms         # = health.timeout_s

frontend fe
    bind :8080
    default_backend be

backend be
    balance ${ALGO}
    option httpchk
    http-check send meth GET uri /health ver HTTP/1.1 hdr Host lb
    http-check expect status 200
    default-server inter 1000 fall 3 rise 2     # = interval_s, fail_threshold, success_threshold
    server b1 10.0.3.11:8000 check
    server b2 10.0.3.12:8000 check
    server b3 10.0.3.13:8000 check
    server b4 10.0.3.14:8000 check

listen stats
    bind :8404
    stats enable
    stats uri /
    stats refresh 1s
```
**Mapping table (put it in `docs/notes_observability.md` and in the README):**
| Our setting | HAProxy line |
|---|---|
| `listen.port: 8080` | `bind :8080` |
| `algorithm: round_robin / least_conn / ip_hash` | `balance roundrobin / leastconn / source` |
| `health.path` | `http-check send ... uri /health` |
| `health.interval_s: 1.0` | `inter 1000` |
| `health.fail_threshold: 3` | `fall 3` |
| `health.success_threshold: 2` | `rise 2` |
| `health.timeout_s: 0.5` | `timeout check 500ms` |
| `timeouts.connect_s` | `timeout connect 2s` |
| `timeouts.read_s` | `timeout server 10s` |
| `timeouts.keepalive_s` | `timeout http-keep-alive 15s` |
| one backend connection per request | `option http-server-close` |
| no retry to another backend | `retries 0` and no `option redispatch` |
| `X-Forwarded-For` | `option forwardfor` |

**Fairness rules:**
- Run the LB container and the HAProxy container with the **same CPU limit** (default `--cpus 1.0`, one variable in `.env`). Only one is active per run.
- Same IP when possible: LB is 10.0.2.10, HAProxy 10.0.2.11, both port 8080.
- `balance source` uses a different hash function than our IP-hash. So the *mapping* of clients to backends will differ. Compare load distribution qualitatively for this algorithm and say so in the report. Do not try to make them match.
- Know the HAProxy detection event: with `log stdout`, HAProxy prints a line like `Server be/b3 is DOWN, reason: ...` (in HAProxy logs, `Layer4 timeout` or `Layer4 connection problem`). Capture it with `docker logs -t` (container timestamps) and parse it. Do **not** use dashboard polling for detection time.

## 11. Part F: Benchmark tooling (`bench/`)

### 11.1 Client image (`bench/client/Dockerfile`)
Based on Ubuntu 22.04. Install `curl`, `iproute2`, `iputils-ping`, `traceroute`, `tcpdump`, `wrk` (apt), and `hey` (download the release binary at build time; pin the version). Name `cn-lb-client:latest`. Talha uses this image for c1-c4 in the topology. Tell him the image name.

### 11.2 Tools
- **Primary: `hey -z 60s -c <n> -o csv http://10.0.2.10:8080/`.** The CSV has one row per completed request with: response time, status code, and the **offset** (seconds since the start of the test). That lets us slice the run into pre-kill (0-30 s) and post-kill (30-60 s) and compute percentiles **per window**.
- `wrk -t2 -c<n> -d60s --latency` is the fallback. It gives only whole-run numbers. If you must use it, state clearly in the output which metrics are whole-run.
- Check hey's CSV column order against the installed version before coding `collect.py` (do not assume).

### 11.3 Concurrency split (decision D11)
IP-hash needs several client IPs. Split the total concurrency across c1-c4, one `hey` process per host, started **at the same time**. Totals: 50 = 13+13+12+12, 200 = 50 x 4, 500 = 125 x 4. Record the split in `summary.json`.

### 11.4 Run timeline (`run_experiment.sh <exp> <system> <algo> <concurrency> <rtt_ms> <run_id>`)
```
 1. Reset:     start all backends (start_backend.sh), wait for /health 200 on b1-b4.
 2. Stop the other system. Start the target (own LB with config.<algo>.json, or HAProxy rendered for <algo>).
               Wait until the target answers GET / with 200 through each client host.
 3. Impairment: topo/set_rtt.sh <rtt_ms>   (Talha's script; 0 = off). Print the active qdisc into the run folder.
 4. Warm-up:   hey -z 5s -c <n/4> on each client. Discard output. Sleep 2 s.
 5. t0:        record t0 = date -u +%s%3N. Take stats snapshot "s0". Start CPU sampler. Start the 4 hey processes (-z 60s -o csv).
 6. t0+30 s:   take stats snapshot "s30", then kill_backend.sh b3 (records kill_ts).
 7. t0+60 s:   hey ends. Take stats snapshot "s60". Stop CPU sampler.
 8. Collect:   copy hey CSVs, stats snapshots, LB log or HAProxy log (docker logs -t), kill_ts, CPU samples, qdisc output.
 9. Restore:   start_backend.sh b3, wait for "UP" (our log: backend_up, HAProxy: stats), sleep 10 s cool-down.
10. Validity checks (11.8). Write summary.json via collect.py.
```
Sleep timings are measured against `t0` using the monotonic clock in the script, not accumulated `sleep` calls.

### 11.5 Output layout
```
results/<exp>/<system>_<algo>_c<N>_rtt<R>_run<id>/
    hey_c1.csv hey_c2.csv hey_c3.csv hey_c4.csv   (or wrk_c*.txt)
    stats_s0.json stats_s30.json stats_s60.json     (our LB /stats, or HAProxy ;csv export)
    lb.jsonl (own LB log)  or  haproxy.log (docker logs -t)
    cpu.csv           # t_s, cpu_pct (per second)
    kill_ts.txt  t0.txt
    qdisc.txt
    environment.txt   # once per experiment, not per run
    summary.json
```

### 11.6 `collect.py` and `summary.json` (fixed schema)
```json
{"exp": "exp2", "system": "own", "algorithm": "round_robin", "concurrency": 200, "rtt_ms": 100, "run": 1,
 "t0_ms": 0, "kill_ms": 0, "down_ms": 0, "detection_s": 0.0,
 "windows": {
   "pre":  {"rps": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "requests": 0, "failed": 0},
   "post": {"rps": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "requests": 0, "failed": 0}},
 "failed_in_detection_window": 0,
 "per_backend_pre":  {"b1": 0, "b2": 0, "b3": 0, "b4": 0},
 "per_backend_post": {"b1": 0, "b2": 0, "b3": 0, "b4": 0},
 "cpu_pct": {"pre_mean": 0.0, "post_mean": 0.0, "peak": 0.0},
 "valid": true, "invalid_reason": null}
```
How each number is computed:
- Merge the four CSVs. Window by `offset`: pre = [0, 30), post = [30, 60). (Offsets are relative to each hey start; all four started within a few ms of t0.)
- `rps` = requests in window / 30. Percentiles: nearest-rank on the window's response times (same function as `metrics.py`).
- `failed` = rows with status >= 500 plus the error count hey reports (socket or timeout errors).
- `down_ms` = timestamp of the first `backend_down` for b3 after `kill_ms` (our log) or the HAProxy `is DOWN` line. `detection_s = (down_ms - kill_ms) / 1000`.
- `failed_in_detection_window` = failed rows whose absolute time (`t0_ms + offset*1000`) lies in [kill_ms, down_ms].
- `per_backend_pre` = `s30` minus `s0` per backend; `per_backend_post` = `s60` minus `s30`.
- `cpu_pct`: see 11.7.

### 11.7 LB CPU
- Sample the **container cgroup** once per second: read `cpu.stat` field `usage_usec` (cgroup v2) or `cpuacct.usage` (v1). CPU % = delta usage / delta wall time x 100 (so 100% = one full core). Find the path with `docker inspect` (container id) or read via `docker exec`.
- Same method for the LB container and the HAProxy container.
- Report mean pre-kill, mean post-kill and peak.

### 11.8 Validity checks (a failing run is marked `valid:false` and rerun)
1. All four CSVs exist and have rows in both windows.
2. Kill happened within t0+30 s +/- 0.5 s.
3. Load generator hosts were not the bottleneck: record `docker stats` or cgroup CPU of c1-c4; mark invalid if any client exceeded 90% of its CPU limit for more than 5 s.
4. No hey errors in the pre window (errors before the kill mean the setup is broken).
5. `detection_s` exists and is between 0 and 10 s.
6. `ulimit -n` was >= 65535 in the shell that started the load (500 connections).

### 11.9 Experiment plan (decision D6, D7, D10)
- **Experiment 1** (load): 3 algorithms x concurrency 50/200/500 x 3 runs x 2 systems = **54 runs**, RTT 0 (no netem).
- **Experiment 2** (network conditions, mandatory): round robin, concurrency 200, RTT 20/100/200 ms x 3 runs x 2 systems = **18 runs**. Reports **all** key metrics. (Optional loss 0/2/5% only if time remains and Talha's `set_loss.sh` exists.)
- `bench/run_all.sh` runs the full plan in a **randomised order with a fixed seed** (record the seed), so slow drift (laptop heat, background load) is not tied to one system. Resume support: skip runs whose `summary.json` has `valid:true`.
- Record `bench/environment.sh` output once per experiment: laptop CPU model and cores, RAM, OS and kernel, Docker version, Python version, hey and HAProxy versions, container CPU limits, `ulimit -n`, git commit hash of the repo.
- Total time: about 72 runs x about 100 s = 2 to 3 hours of unattended runtime. Make it restartable.

### 11.10 `analyze.py`
- Group runs by (exp, system, algorithm, concurrency, rtt). For each metric and window compute: mean, sample standard deviation (n-1), min, max over the 3 runs. Mean of per-run values, never of raw samples.
- Outputs (CSV, one row per condition): `results/tables/exp1.csv`, `exp2.csv` with columns for every metric, pre and post.
- Plots (matplotlib, PNG, readable, labelled axes with units, error bars = std dev, own LB vs HAProxy on the same axes):
  1. requests/s vs concurrency (per algorithm),
  2. p95 latency vs concurrency,
  3. requests per backend (stacked bars, per algorithm, pre and post kill),
  4. failure detection time (own vs HAProxy, Experiment 2 conditions),
  5. LB CPU vs concurrency.
- Print a one-page text summary of the headline comparisons for the report.

## 12. Known behaviours: document them, do not "fix" them
1. **What `docker kill` does to the network.** The container's network namespace disappears. Packets to its IP may be answered with ICMP host-unreachable after ARP fails, or get no answer at all, instead of a TCP reset ("connection refused"). We **expect** probes to fail as `timeout` or `unreachable` rather than `refused`. Therefore requests sent to the dead backend during the detection window may hang until `connect_s` (2 s) before failing. This inflates p99 latency in the post window. **Verify it at Checkpoint 2**: after killing b3, run `curl -m 5 -v http://10.0.3.13:8000/` from a client host and record what you see in `docs/notes_observability.md`. Tell Talha and Sufyan the result in your report-back (it changes how the failure reasons read in the report).
2. **IP-hash with four clients.** Computed mapping with SHA-256 over the four client IPs and 4 healthy backends: c1 (10.0.1.11) -> b3, c2 -> b2, c3 -> b2, c4 -> b2. So b2 receives about 75% of requests and b1 and b4 receive none. After b3 dies (3 backends: b1, b2, b4): c1 -> b1, c2 -> b1, c3 -> b2, c4 -> b2. Consequences: c1's traffic is exactly the traffic that hits the killed backend (25% of load), and failed requests will be about 25% in the window. This is a property of IP-hash with few clients. Report it as a finding. **Do not change the hash or the client count to look even.**
3. **Python LB slower than HAProxy.** Expected. It is a finding, not a failure.
4. **Detection time is not exactly 3 s.** Expected range 2 to 3.5 s (see Sufyan's brief).
5. **Warm-up.** First 5 s of load are discarded (separate warm-up call, not part of the 60 s).
6. **Shared laptop.** Load generator, LB and backends share one machine. Pin CPUs with container limits and record them. State the limitation in the report.

## 13. Edge cases you must handle
1. Logger: unserialisable field value, directory missing, disk full, 10,000 concurrent callers (no interleaved lines).
2. Metrics: `latency_ms` of 0, `backend_id=None`, negative bytes (clamp at 0), snapshot while empty (all zeros, no division by zero).
3. Stats server: client closes mid-response; request line longer than 8 KB; 1000 `/stats` requests in 10 s.
4. Backend: client disconnects during the delay; `DELAY_MS` invalid (fail fast at start with a clear message).
5. Bench: kill script fails (container not running) so the run is invalid; `hey` produces no CSV; kill timestamp file missing; two runs started at once (use a lock file).
6. HAProxy: backend killed twice; stats page unreachable.
7. Time: all timestamps come from the same host clock (milliseconds since epoch). Never mix clocks from different machines.

## 14. Acceptance tests
1. **Logger:** valid JSON on every line; concurrent calls never interleave; unserialisable values do not raise; levels filter correctly; `backend_down` is written at `WARNING` level.
2. **Metrics:** percentile function correct on a known set (for example 1..100 gives p50=50, p95=95, p99=99); counters correct after 10,000 `request_done` calls; snapshot has exactly the schema keys; bounded memory after 250,000 calls.
3. **Stats server:** `/stats` valid JSON; `/` serves the dashboard; `/healthz` 200; unknown path 404; works under load (use a small asyncio load script).
4. **Dashboard:** loads, updates every second, shows DOWN in red **with the word**, shows the banner when `/stats` stops.
5. **Backend:** `/health` stays fast while `/` waits 50 ms (measure under 20 concurrent requests); keep-alive serves 3 requests on one connection; delay is configurable.
6. **HAProxy:** all three algorithms render and start; killing b3 marks it DOWN in about 3 s (stats page on 8404); config check passes.
7. **Collect/analyze:** `tests/test_collect.py` feeds a small synthetic CSV (known answers) and checks windowing, percentiles, failed counts and detection time.
8. **Bench dry run:** one end-to-end run with concurrency 50 and `-z 10s` produces a populated results folder and a table row.

## 15. Things you need from others, and what you give
| From | What |
|---|---|
| Talha | `topo/set_rtt.sh <ms>` (0 = off), `topo/set_loss.sh`, container names for the topology, the client hosts c1-c4 using `cn-lb-client:latest`, backends from `cn-lb-backend:latest` |
| Sufyan | `load_config`, `make_pool`; `pool.all()` objects with `.id`, `.healthy`, `.active`; the failure-reason vocabulary |

You give: `make_logger`, `make_metrics`, `start_stats_server`, `cn-lb-backend` image, `cn-lb-client` image, the HAProxy config, the bench scripts, and the delay/CPU table for the topology file. Until teammates' code lands, test with fakes (a fake pool with three attributes is enough).

## 16. Build order and rough effort
1. Backend app, Dockerfile, compose, kill/start scripts (2 h). Share the image name and the delay table with Talha first.
2. Logger + metrics + `tests/test_obs.py` (3 h).
3. Stats server + dashboard (3 h).
4. HAProxy template, render script, mapping table (2 h).
5. Client image, `collect.py` + tests on synthetic data (3 h).
6. `run_experiment.sh`, CPU sampler, validity checks, dry run (4 h).
7. `analyze.py` and plots (3 h).
8. `run_all.sh`, `environment.sh` (1 h).
9. `docs/notes_observability.md` (1 h).
Commit after each step. Open the PR when the tests pass. The full experiment run happens in Phase 4 after integration.

## 17. Git
- Branch `feat/observability`. PR to `main`. No direct pushes to `main`.
- Commit messages: short, present tense, say what changed (`add percentile metrics`).
- PR description: what is included, tests passing, assumptions, open questions.
- AI use is reported manually by the group in one general statement. Nothing to log in the repo.

## 18. Definition of done
- [ ] All tests pass locally.
- [ ] `lb/obs/` has no third-party imports. Python 3.9 compatible.
- [ ] No globals, singletons, hard-coded IPs or ports.
- [ ] Type hints and short docstrings on public functions.
- [ ] `lb/interfaces.py` untouched.
- [ ] Both Docker images build. Dry run works.
- [ ] `docs/notes_observability.md` written (section 21).
- [ ] PR opened with the report-back (section 22).

## 19. Rules
- Python 3.9+. `lb/obs/` is standard library only. Third-party packages are fine inside `bench/` and `backends/` (psutil, matplotlib).
- No globals, singletons, hard-coded IPs or ports. Config or arguments only. (The topology's fixed IPs appear in config files and env vars, not in Python code.)
- Do not change `lb/interfaces.py`. If it blocks you, write the problem down and tell the group.
- Never block the event loop in `lb/obs/`.
- Stay inside this brief. Scope comes from the official course text.

## 20. Not to build
Prometheus or Grafana, external dashboard frameworks, a database, log shipping, alerting, authentication on the stats page, persistent metrics, histograms with fancy buckets, a custom load generator, auto-scaling, request tracing, new config keys. If you think one is needed, ask first.

## 21. Explain as you go (important)
The group must understand each block. Write `docs/notes_observability.md`: about 10 plain-English lines per component (logger, metrics, stats server, dashboard, backend, HAProxy config, run script, collect, analyze). Each covers what it measures, why that metric matters for a load balancer, and how a number in the final report is produced from it. Explain LB-specific ideas: percentile latency, closed-loop load, failure detection time, why pre-kill and post-kill are separated, why HAProxy is the baseline, why the CPU limit is the same for both, what the `docker kill` check showed. Skip CN basics.

## 22. Report back
List: files created, tests passing (count), assumptions, deviations from this brief and why, the result of the `docker kill` network check, the `hey` CSV column order you found, open questions for Talha (topology, set_rtt, client hosts) and Sufyan (pool, failure reasons), and any place the frozen interface made your job harder.
