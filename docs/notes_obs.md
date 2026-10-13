# Notes: observability, backends, HAProxy, bench (Abdur Rehman)

## Work record

This file tracks what has been implemented and verified so far. It does not
claim that the routed-topology experiments or benchmark tooling are complete.

### Completed

1. Added the asyncio backend server, Dockerfile, Compose setup for four backends,
   and kill/start helper scripts. Backends have configured delays and a fast,
   delay-free `/health` endpoint.
2. Added the JSON-lines event logger. It timestamps events when called, filters
   event levels, and always writes `backend_down` and `backend_up`.
3. Added request counters, per-backend counters, bounded latency samples, and
   nearest-rank p50/p95/p99 metrics.
4. Added the asyncio stats server and dashboard. The stats routes and response
   behavior are covered by tests; the dashboard reads `/stats` once per second.
5. Added the HAProxy 2.8 template and renderer. It maps the project algorithms
   to HAProxy's `roundrobin`, `leastconn`, and `source`, with matching health
   checks, timeouts, forwarding, and no retry/redispatch.
6. Fixed Git Bash path conversion in the renderer so Docker receives the
   container's `/usr/local/etc/haproxy/haproxy.cfg` path rather than a rewritten
   Windows path.
7. Kept the temporary `demo_dashboard.py` out of the repository and commits.

### Verification record

- The observability/statistics tests passed: 42 tests.
- After adding HAProxy support, the full currently available suite passed:
  48 passed, 3 skipped. The skipped cases require POSIX shell integration;
  Docker-based validation was run separately.
- Rendered and checked all three algorithm configs with the official
  `haproxy:2.8` Docker image. HAProxy reported `Configuration file is valid`
  for `round_robin`, `least_conn`, and `ip_hash`.
- The generated `haproxy/haproxy.cfg` is ignored by Git. It is a local
  generated output, not a source file to commit.
- Commit `ddc11f2` (`Add HAProxy config renderer`) was pushed to
  `feat/observability`.

### Added after the first integration (bench tooling)

8. `bench/client/Dockerfile` (image `cn-lb-client:latest`), `bench/make_configs.py`,
   `bench/run_experiment.sh`, `bench/run_all.sh`, `bench/collect.py`,
   `bench/analyze.py`, `bench/cpu_sampler.py`, `bench/environment.sh` and
   `tests/test_collect.py`.
9. `backends/kill_backend.sh` and `start_backend.sh` now compute milliseconds as
   `$(( $(date -u +%s%N) / 1000000 ))`. `date +%s%3N` is not portable: the uutils
   `date` shipped with Ubuntu 25.10+ ignores the width and prints 19 digits, so the
   script stopped before `docker kill`.

### Verification record (bench)

- `tests/test_collect.py`: 8 tests on synthetic runs with known answers
  (windows, nearest-rank percentiles, failed counts, detection time for the own LB
  log and the HAProxy log, per-backend counts, CPU means, each validity check,
  mean and sample standard deviation).
- Dry run on the routed topology, 10 s instead of 60 s, concurrency 50, WSL2:
  `own round_robin rtt 0` and `haproxy round_robin rtt 20` both produced a full run
  folder and `valid: true`; `analyze.py` wrote both tables and all five plots.
  These dry-run numbers are only a check that the pipeline works. They are not
  results and must not appear in the report.
- Column order of `hey -o csv` (hey v0.1.4):
  `response-time,DNS+dialup,DNS,Request-write,Response-delay,Response-read,status-code,offset`
  (times in seconds). `collect.py` finds the columns by name.

### Still outstanding

- The full plan (72 runs) has not been run. Run `ulimit -n 65535` and then
  `bench/run_all.sh` after the topology is deployed (see "How to run" below).
- The `docker kill` network check (brief section 12.1) is not written up yet.
- Detection time for the own LB was 2.8 s and for HAProxy 2.8 s in the dry runs; the
  real values come from the full runs.

## How the benchmark pipeline works

**Run script (`run_experiment.sh`).** One run resets all backends, restarts the
system under test with the chosen algorithm, applies the RTT with `topo/set_rtt.sh`,
runs a 5 s warm-up that is thrown away, then starts four `hey` processes (one per
client, concurrency split 13+13+12+12 for 50) at t0. At t0+30 s it takes a stats
snapshot and kills b3 with `docker kill`; at t0+60 s it takes the last snapshot.
Times are measured against the monotonic clock, not by adding up sleeps. Closed-loop
load means each connection sends the next request only after the previous answer,
so throughput depends on latency; this is why the same concurrency gives different
requests per second for the two systems.

**Why pre-kill and post-kill are separate windows.** The kill changes the system
(three backends instead of four, some failed requests until the health check
notices). Mixing both halves would hide that effect, so every metric is reported
for [0, 30) s and [30, 60) s.

**Collect (`collect.py`).** Merges the four hey CSVs and cuts them by offset.
Percentiles use the nearest-rank rule, the same as `lb/obs/metrics.py`. A request
counts as failed when its status is 500 or higher. Failure detection time is the
first `backend_down` event for b3 after the kill timestamp (own LB log) or the first
`Server be/b3 is DOWN` line (HAProxy log from `docker logs -t`), minus the kill
timestamp, so startup events and other backends are ignored. `hey -o csv` writes only
requests that got a response, so a transport error would not appear; both systems
answer 502/503/504 when a backend is unreachable, which is what the failover window
contains. Requests per backend come from the difference between the stats snapshots.

**Validity.** A run is marked `valid: false` (and `run_all.sh` repeats it up to three
times) when a CSV is missing or a window is empty, the kill was not at t0+30 s
(+/- 0.5 s), requests failed before the kill, detection time is missing or above
10 s, a client used more than 90 % of its CPU limit for more than 5 s, or the
open-file limit was below 65535.

**CPU.** `cpu_sampler.py` reads the container's cgroup CPU counter from the host once
per second, so sampling does not disturb the load. 100 % is one full core. The own LB
and HAProxy have the same limit (`LB_CPUS`, default 1.0), so a CPU comparison is fair.
The client limit used in the validity check is `CLIENT_CPU_LIMIT` (default 1.0 core);
the topology file does not set a client limit yet, so this check is only meaningful
if you also set one.

**Analyze (`analyze.py`).** Groups valid runs by experiment, system, algorithm,
concurrency and RTT, and computes mean, sample standard deviation (n-1), minimum and
maximum over the runs (mean of per-run values, never of raw samples). It writes
`results/tables/exp1.csv`, `exp2.csv` and five plots with standard-deviation error
bars, and prints a short comparison.

## How to run

```bash
# one-time: images and topology
docker build -t cn-lb-backend:latest backends
docker build -t cn-lb-client:latest bench/client
python3 bench/make_configs.py && mkdir -p results && cp results/configs/config.round_robin.json results/lb_config.json
LB_CONFIG=$PWD/results/lb_config.json bash topo/build.sh

# one run (10 s dry run), then everything
HEY_DURATION=10 bash bench/run_experiment.sh exp1 own round_robin 50 0 1
ulimit -n 65535 && SEED=1 bash bench/run_all.sh
python3 bench/analyze.py
```

The topology must be deployed with `LB_CONFIG` pointing at `results/lb_config.json`:
`run_experiment.sh` rewrites that file for every algorithm and restarts the LB
container, and refuses to run if the container does not see the same file.

## HAProxy reference mapping

`haproxy/haproxy.cfg.tmpl` keeps HAProxy's HTTP health checks and timeouts aligned
with the load balancer. Run `bash haproxy/render.sh <algorithm>` to render the
config and check it with the official `haproxy:2.8` image before use.

| Load-balancer setting | HAProxy setting |
|---|---|
| `round_robin` / `least_conn` / `ip_hash` | `balance roundrobin` / `leastconn` / `source` |
| Port 8080 | `frontend fe` bind |
| `/health`, interval 1 s, 3 failures down, 2 successes up | HTTP check, `inter 1000 fall 3 rise 2` |
| Probe timeout 0.5 s | `timeout check 500ms` |
| Connect/read/keep-alive timeouts 2/10/15 s | `timeout connect 2s`, `timeout server 10s`, `timeout http-keep-alive 15s` |
| New backend connection per request | `option http-server-close` |
| No retry or redispatch | `retries 0`; no `option redispatch` |
| Forward client address | `option forwardfor` |

The HAProxy `source` algorithm hashes client addresses but does not promise the
same backend mapping as the project's SHA-256 IP-hash implementation.
