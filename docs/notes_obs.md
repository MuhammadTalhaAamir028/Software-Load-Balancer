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

### Still outstanding

The live HAProxy traffic/failover test and routed-topology verification have
not been completed. The client image, config-generation helper for the own LB,
benchmark collection/run/analysis scripts, and full observability notes remain
future work. No benchmark measurements or routed-topology results are claimed
here.

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
