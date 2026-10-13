# Software Load Balancer with Health Checks (CS-3001 Project 5)

Python asyncio L7 reverse proxy with round robin, least connections and IP-hash, active health checks, benchmarked against HAProxy on a routed topology.

Team: Talha, Sufyan, Abdur Rehman.

Start here: `docs/PRD.md`, then `docs/RESPONSIBILITIES.md`, then your brief in `docs/briefs/`.

Reproduction steps (setup, run, experiments) are filled in during Phase 4.

## HAProxy reference configuration

With Docker running, render and validate one HAProxy config from the repository
root:

```bash
bash haproxy/render.sh round_robin
```

Use `least_conn` or `ip_hash` to select the other algorithms. The script checks
the generated file with the official `haproxy:2.8` image before saving it as
`haproxy/haproxy.cfg`.

| Load-balancer setting | HAProxy setting |
|---|---|
| Frontend port 8080 | `frontend fe`, `bind :8080` |
| HAProxy stats port 8404 | `listen stats`, `bind :8404` |
| `round_robin` / `least_conn` / `ip_hash` | `balance roundrobin` / `leastconn` / `source` |
| `/health`, 1 s interval, 3 failures down, 2 successes up | HTTP check, `inter 1000 fall 3 rise 2` |
| 0.5 s health-check timeout | `timeout check 500ms` |
| 2/10/15 s connect/read/keep-alive timeouts | `timeout connect 2s`, `timeout server 10s`, `timeout http-keep-alive 15s` |
| One backend connection per request | `option http-server-close` |
| No retry or redispatch | `retries 0`; no `option redispatch` |
| Forward client address | `option forwardfor` |

HAProxy's `source` balance uses a different hash mapping from the project's
IP-hash algorithm, so compare its distribution qualitatively.
