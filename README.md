# Software Load Balancer with Health Checks (CS-3001 Project 5)

Python asyncio L7 reverse proxy with round robin, least connections and IP-hash, active health checks, benchmarked against HAProxy on a routed topology.

Team: Talha, Sufyan, Abdur Rehman.

Start here: `docs/PRD.md` (scope and decisions), then the reproduction steps below.

## What this project is

A Python asyncio layer-7 reverse proxy (standard library only) that spreads HTTP/1.1 requests over four backends with round robin, least connections or IP-hash, checks every backend with `GET /health` once a second (DOWN after 3 failures, back after 2 successes), and is benchmarked against HAProxy. Everything runs on a routed lab built from containers: 3 subnets, 2 routers (FRRouting), 4 clients, our load balancer, HAProxy and 4 backends. Nothing runs on localhost.

| Folder | What is in it |
|---|---|
| `lb/` | The load balancer (`core/` proxy, `balancing/` algorithms, pool and health checker, `obs/` logs, metrics, stats server) |
| `topo/` | containerlab topology, FRR configs, static routes, netem scripts, capture script, `verify.sh` |
| `backends/`, `haproxy/` | The four backend servers and the HAProxy reference configuration |
| `bench/` | Load generator image, one-run and full-plan scripts, collection and analysis |
| `ui/` | The live browser UI and its relay |
| `captures/`, `docs/img/`, `docs/evidence/` | Packet captures, annotated screenshots, measured evidence |
| `docs/report/` | The technical report |
| `tests/` | 222 unit tests |

## Reproduce the project

Follow these steps in order. Steps 1 to 4 take about 25 minutes the first time (mostly downloads), later starts about 1 minute. No cloud account, no API key and no paid software are needed.

### 1. What you need

| Item | Needed | Tested with |
|---|---|---|
| PC | 4+ CPU cores, 8+ GB RAM (16 GB better for the 500-connection runs), 30 GB free disk | Windows 11 Home |
| OS | Windows 10/11 with WSL2, **or** native Ubuntu 24.04 (PC or VM) | WSL2 Ubuntu 24.04, kernel 6.18 |
| Linux kernel | A real Linux kernel (network namespaces, bridges, `tc netem`). WSL2 provides one; Windows alone cannot run the lab | |
| Docker Engine | installed inside Ubuntu (not Docker Desktop) | 29.x |
| containerlab | | 0.79.0 |
| Container images | pulled or built by the scripts | FRR `quay.io/frrouting/frr:10.2.1`, `haproxy:2.8`, `python:3.11-slim`; `cn-lb-client` (hey v0.1.4 built from source) and `cn-lb-backend` |
| Other tools | tshark, tcpdump, Graphviz (diagram only) | tshark 4.2 |
| Python | 3.9+ (the load balancer uses only the standard library) | 3.12 |

### 2. Get the code

```bash
git clone https://github.com/MuhammadTalhaAamir028/Software-Load-Balancer.git
cd Software-Load-Balancer
```

On Windows keep the clone anywhere (for example `C:\work\slb`); the start script copies it into Linux. On native Linux work from your home folder, not `/mnt/...`.

### 3. Install and start

- **Windows (one click):** double-click `start.bat` (or run `python start.py`). It installs WSL and Ubuntu 24.04 if needed, installs Docker, containerlab and tshark inside Ubuntu, builds the images, deploys the lab and starts the UI at <http://127.0.0.1:8765/>. It may ask for administrator rights and a restart; run it again afterwards. Later starts: double-click `quick_start.bat` (about a minute, keep its window open).
- **Linux / by hand:** install Docker, containerlab and tshark, then follow sections 1 to 3 of [START_PROJECT.md](START_PROJECT.md): build the two images, `bash haproxy/render.sh round_robin`, `bash topo/build.sh`.

### 4. Check that it works

```bash
bash topo/verify.sh
```

You should see the routing tables of R1 and R2, a traceroute from c1 to b1 that lists `10.0.1.1`, `10.0.2.2` and `10.0.3.11` (two routers in between), and a `200` from the load balancer. Our own output is in `docs/evidence/checkpoint1.txt`. If the traceroute shows `*` after the first hop, rebuild the lab (`bash topo/build.sh`); see the troubleshooting table in START_PROJECT.md.

### 5. Run the proof of concept

1. Open the UI at <http://127.0.0.1:8765/>. It shows the topology, routing proof, a live request, a live packet capture, metrics, failover, the HAProxy comparison and a guided demo (panel U13).
2. In U13 use **Start load**, wait 20 seconds, then **Kill b3**. The load balancer marks b3 DOWN after about 3 seconds (the log says `3 consecutive failures: refused`) and traffic moves to b1, b2 and b4. **Restore b3** brings it back after two good probes.
3. Switch the algorithm (round robin, least connections, IP-hash) for either system in U13. **Own LB**, **HAProxy** or **Both (demo)** choose which system receives the load; Both is for watching side by side only (they share the CPU, so its numbers are not report data).
4. By hand: `docker exec clab-lb-c1 curl -i http://10.0.2.10:8080/` (own LB) and `http://10.0.2.11:8080/` (HAProxy). The statistics are at `http://10.0.2.10:9090/stats`.

### 6. Repeat the experiments

The plan is 72 scripted runs: Experiment 1 (54 runs) = 2 systems x 3 algorithms x 50/200/500 connections x 3 repeats; Experiment 2 (18 runs) = 2 systems x RTT 20/100/200 ms x 3 repeats at 200 connections with round robin. Each run: 5 s warm-up (discarded), 60 s of load from c1 to c4, `docker kill` of b3 at t = 30 s, pre- and post-kill reported separately. Stop the UI first: experiments must run alone and one system at a time.

```bash
ulimit -n 65535                                   # needed for the 500-connection runs
python3 bench/make_configs.py --out-dir results/configs
cp results/configs/config.round_robin.json results/lb_config.json
LB_CONFIG=$PWD/results/lb_config.json bash topo/build.sh       # deploy with that config
bash haproxy/render.sh round_robin && docker restart -t 1 clab-lb-haproxy
bash bench/run_all.sh --list                      # print the 72-run plan (nothing runs)
SEED=1 bash bench/run_all.sh                      # about 2 to 2.5 hours; safe to stop and run again
python3 bench/analyze.py                          # tables in results/tables, plots in results/plots
```

A run writes `results/<exp>/<system>_<algo>_c<N>_rtt<R>_run<k>/summary.json` (and raw logs that stay local). `bench/run_all.sh` skips runs that are already valid and repeats invalid ones up to 3 times. Hardware and tool versions are saved in `results/exp1/environment.txt`. `results/checkpoint2/` is a single dry run, not report data.

### 7. Packet captures

`captures/README.md` lists the exact command and Wireshark filter for each exchange (client to load balancer, load balancer to backend, health probe, failover, and the HAProxy equivalents). Example: `bash topo/capture.sh client_lb_http lb eth1 "tcp port 8080" 5`. Annotated screenshots of our captures are in `docs/img/`.

### 8. Tests

```bash
python -m unittest discover -s tests -t .          # 222 tests, about 50 seconds, runs on Windows or Linux
```

### 9. Stop

`bash topo/teardown.sh` removes the lab; close the `quick_start.bat` window (or run `python start.py --stop`) to stop the UI. Remove network impairment with `bash topo/set_rtt.sh 0 && bash topo/set_loss.sh 0`.

### Good to know

- WSL2 shuts down when nothing runs in it and the lab loses its links; `quick_start.bat` keeps WSL alive, otherwise keep a terminal open and rebuild with `bash topo/build.sh` if the traceroute breaks.
- Shell and config files must have Linux (LF) line endings; the repository enforces this in `.gitattributes`.
- Two WSL distros share one network, so only one of them may run Docker at a time.
- Full step-by-step commands and a troubleshooting table: [START_PROJECT.md](START_PROJECT.md). Design decisions and the experiment plan: `docs/PRD.md`.

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
