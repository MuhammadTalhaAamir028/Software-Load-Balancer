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

## Topology and captures

Everything runs in WSL2 Ubuntu from a Linux clone (`~/slb`, not `/mnt/...`). Needs Docker, containerlab 0.79+, `tshark` (for `editcap`) and
`graphviz`. Details: `docs/notes_topo.md`, `captures/README.md`. Diagram: `docs/architecture.png` (source `docs/architecture.dot`).

```bash
# images (once)
docker build -t cn-lb-client:latest bench/client
docker build -t cn-lb-backend:latest backends

# build the routed topology (c1-c4, R1, R2, own LB 10.0.2.10, HAProxy 10.0.2.11, b1-b4 10.0.3.11-.14)
bash topo/build.sh

# Checkpoint 1 proof: routes, traceroute c1 -> b1 through both routers, curl through the LB
bash topo/verify.sh | tee docs/evidence/checkpoint1.txt

# impairment on R1 (round trip client <-> LB), 0 removes it
bash topo/set_rtt.sh 100
bash topo/set_loss.sh 5
bash topo/set_rtt.sh 0 && bash topo/set_loss.sh 0

# packet captures: see captures/README.md for each command, then
editcap -F pcapng captures/<name>.pcap captures/<name>.pcapng

# HAProxy needs its config rendered, then a node restart
bash haproxy/render.sh round_robin && docker restart clab-lb-haproxy

# tear down
bash topo/teardown.sh
```

Measured results of the topology tests are in `docs/evidence/` (`checkpoint1.txt`, `docker_kill_behaviour.md`, `capture_verification.txt`).
