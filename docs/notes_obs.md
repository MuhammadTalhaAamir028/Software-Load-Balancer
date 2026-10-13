# Notes: observability, backends, HAProxy, bench (Abdur Rehman)
What each log event means, how percentiles are computed, how the kill and detection time are measured, one failure case.

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
