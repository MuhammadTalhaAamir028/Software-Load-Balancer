# Start Project 5

This guide starts the routed topology, own load balancer, HAProxy, dashboard relay, and live UI.

All commands must run in WSL2 Ubuntu or a Linux VM. Use a Linux filesystem clone such as `~/slb`, not `/mnt/c/...`.

## 1. Prerequisites

Open a Linux terminal, move to the repository, and verify the required tools:

```bash
cd ~/slb

docker version
containerlab version
python3 --version
git status --short
```

Install Docker and Containerlab before continuing. The project does not expose or support `localhost` for its routed topology.

Verify or build the custom client and backend images:

```bash
docker image inspect cn-lb-client:latest
docker image inspect cn-lb-backend:latest
```

If either image is missing, build it:

```bash
docker build -t cn-lb-client:latest bench/client
docker build -t cn-lb-backend:latest backends
```

Pull or verify the remaining images:

```bash
docker image inspect quay.io/frrouting/frr:10.2.1
docker image inspect python:3.11-slim
docker image inspect haproxy:2.8
```

Docker and Containerlab can pull missing images automatically. `tshark` is required for packet decoding:

```bash
tshark --version
```

## 2. Build the topology

Render the default HAProxy configuration:

```bash
bash haproxy/render.sh round_robin
```

Deploy the routed topology and static routes:

```bash
bash topo/build.sh
```

Run the topology proof:

```bash
bash topo/verify.sh
```

Expected proof includes:

- R1 and R2 routing tables.
- A traceroute from c1 to b1 through R1 and R2.
- A successful HTTP request from c1 to the own load balancer.

## 3. Start and verify the load balancers

The own load balancer should already start with the topology.

Verify the own load balancer:

```bash
docker logs --tail 50 clab-lb-lb
docker exec clab-lb-c1 curl -i http://10.0.2.10:8080/
docker exec clab-lb-c1 curl -s http://10.0.2.10:9090/stats
docker exec clab-lb-c1 curl -s http://10.0.2.10:9090/healthz
```

Restart HAProxy after rendering its configuration:

```bash
docker restart -t 1 clab-lb-haproxy
docker exec clab-lb-c1 curl -s 'http://10.0.2.11:8404/;csv'
docker logs --tail 50 --timestamps clab-lb-haproxy
```

Check all backend health endpoints:

```bash
docker exec clab-lb-c1 curl -s http://10.0.3.11:8000/health
docker exec clab-lb-c1 curl -s http://10.0.3.12:8000/health
docker exec clab-lb-c1 curl -s http://10.0.3.13:8000/health
docker exec clab-lb-c1 curl -s http://10.0.3.14:8000/health
```

## 4. Start the live UI

Open a second WSL terminal, move to the repository, and start the relay:

```bash
cd ~/slb
python3 ui/relay.py
```

Open:

```text
http://127.0.0.1:8765/
```

The relay must remain running while using the live dashboard.

Useful relay endpoints:

```bash
curl -s http://127.0.0.1:8765/api/status
curl -s http://127.0.0.1:8765/api/topology
curl -s http://127.0.0.1:8765/api/routes
curl -s http://127.0.0.1:8765/api/netem
curl -s http://127.0.0.1:8765/api/request
curl -s http://127.0.0.1:8765/api/stats
curl -s http://127.0.0.1:8765/api/haproxy
curl -s http://127.0.0.1:8765/api/backends
curl -s http://127.0.0.1:8765/api/logs
curl -s http://127.0.0.1:8765/api/results
curl -s http://127.0.0.1:8765/api/environment
curl -s http://127.0.0.1:8765/api/annotations
```

The existing dashboard can also be checked directly from a topology client:

```bash
docker exec clab-lb-c1 curl -s http://10.0.2.10:9090/
```

## 5. Apply network conditions

Set RTT to 20 ms:

```bash
bash topo/set_rtt.sh 20
docker exec clab-lb-c1 ping -c 3 10.0.2.10
```

Set RTT to 100 ms:

```bash
bash topo/set_rtt.sh 100
docker exec clab-lb-c1 ping -c 3 10.0.2.10
```

Set RTT to 200 ms:

```bash
bash topo/set_rtt.sh 200
docker exec clab-lb-c1 ping -c 3 10.0.2.10
```

Set optional packet loss:

```bash
bash topo/set_loss.sh 2
bash topo/set_loss.sh 5
```

Remove all network impairment:

```bash
bash topo/set_rtt.sh 0
bash topo/set_loss.sh 0
```

Inspect R1 netem:

```bash
docker exec clab-lb-r1 tc qdisc show dev eth1
docker exec clab-lb-r1 tc qdisc show dev eth2
```

## 6. Start and stop live load

Start fixed load from all four clients:

```bash
docker exec -d clab-lb-c1 sh -c 'hey -z 60s -c 13 http://10.0.2.10:8080/'
docker exec -d clab-lb-c2 sh -c 'hey -z 60s -c 13 http://10.0.2.10:8080/'
docker exec -d clab-lb-c3 sh -c 'hey -z 60s -c 12 http://10.0.2.10:8080/'
docker exec -d clab-lb-c4 sh -c 'hey -z 60s -c 12 http://10.0.2.10:8080/'
```

Stop fixed load:

```bash
docker exec clab-lb-c1 pkill -x hey 2>/dev/null || true
docker exec clab-lb-c2 pkill -x hey 2>/dev/null || true
docker exec clab-lb-c3 pkill -x hey 2>/dev/null || true
docker exec clab-lb-c4 pkill -x hey 2>/dev/null || true
```

Watch live metrics in the relay:

```bash
watch -n 1 'curl -s http://127.0.0.1:8765/api/stats'
```

Stop `watch` with `Ctrl+C`.

## 7. Switch the own load-balancer algorithm

Generate the three own-LB configurations:

```bash
python3 bench/make_configs.py --out-dir results/configs
```

Select round robin:

```bash
docker cp results/configs/config.round_robin.json clab-lb-lb:/etc/lb/config.json
docker restart -t 1 clab-lb-lb
```

Select least connections:

```bash
docker cp results/configs/config.least_conn.json clab-lb-lb:/etc/lb/config.json
docker restart -t 1 clab-lb-lb
```

Select IP hash:

```bash
docker cp results/configs/config.ip_hash.json clab-lb-lb:/etc/lb/config.json
docker restart -t 1 clab-lb-lb
```

Verify the active algorithm:

```bash
docker exec clab-lb-c1 curl -s http://10.0.2.10:9090/stats
```

## 8. Switch the HAProxy algorithm

Select round robin:

```bash
bash haproxy/render.sh round_robin
docker restart -t 1 clab-lb-haproxy
```

Select least connections:

```bash
bash haproxy/render.sh least_conn
docker restart -t 1 clab-lb-haproxy
```

Select source/IP hash:

```bash
bash haproxy/render.sh ip_hash
docker restart -t 1 clab-lb-haproxy
```

Verify HAProxy:

```bash
docker exec clab-lb-c1 curl -s 'http://10.0.2.11:8404/;csv'
docker logs --tail 20 --timestamps clab-lb-haproxy
```

HAProxy `source` is not guaranteed to have the exact same hash mapping as the project's IP-hash implementation.

## 9. Switch the active system

Make the own load balancer active:

```bash
docker restart -t 1 clab-lb-lb
docker stop -t 1 clab-lb-haproxy
```

Make HAProxy active:

```bash
docker restart -t 1 clab-lb-haproxy
docker stop -t 1 clab-lb-lb
```

Only one system should receive the test traffic at a time.

## 10. Test backend failover

Start load or keep the live load running.

Kill b3:

```bash
BACKEND_PREFIX=clab-lb- \
KILL_TS_FILE=kill_ts.txt \
bash backends/kill_backend.sh b3
```

Observe health state and events:

```bash
docker exec clab-lb-c1 curl -s http://10.0.2.10:9090/stats
tail -n 20 logs/lb.jsonl
```

b3 should become DOWN after three consecutive failed health checks.

Restore b3:

```bash
BACKEND_PREFIX=clab-lb- \
BACKEND_PORT=8000 \
bash backends/start_backend.sh b3
```

b3 should return UP after two consecutive successful health checks.

## 11. Capture packets

Create a capture through the UI or directly:

```bash
bash topo/capture.sh ui_live lb eth1 "tcp port 8080" 5
```

Decode the capture:

```bash
tshark -n -r captures/ui_live.pcap
```

Decode HTTP and TCP fields:

```bash
tshark -n -r captures/ui_live.pcap \
  -Y "tcp or http" \
  -T fields \
  -e frame.number \
  -e frame.time_relative \
  -e ip.src \
  -e ip.dst \
  -e tcp.flags \
  -e tcp.stream \
  -e http.request.method \
  -e http.response.code
```

Export the capture in pcapng format if needed:

```bash
editcap -F pcapng captures/ui_live.pcap captures/ui_live.pcapng
```

## 12. Run experiments

Set the required open-file limit:

```bash
ulimit -n 65535
```

Inspect the 72-run experiment plan:

```bash
bash bench/run_all.sh --list
```

Run one shortened dry run:

```bash
HEY_DURATION=15 \
bash bench/run_experiment.sh exp1 own round_robin 50 0 1
```

Dry-run results must be labeled dry-run and must not be used as final report evidence.

Run the complete planned experiment suite:

```bash
SEED=1 bash bench/run_all.sh
```

If interrupted, rerun the same command. It skips valid completed runs and retries invalid runs.

Generate tables and plots after valid experiment results exist:

```bash
python3 bench/analyze.py
python3 bench/collect.py --help
```

Run integrity and environment collection:

```bash
mkdir -p results/exp1
mkdir -p results/exp2
bash bench/environment.sh > results/exp1/environment.txt
bash bench/environment.sh > results/exp2/environment.txt
```

## 13. Stop the project

Stop fixed load:

```bash
docker exec clab-lb-c1 pkill -x hey 2>/dev/null || true
docker exec clab-lb-c2 pkill -x hey 2>/dev/null || true
docker exec clab-lb-c3 pkill -x hey 2>/dev/null || true
docker exec clab-lb-c4 pkill -x hey 2>/dev/null || true
```

Remove network impairment:

```bash
bash topo/set_rtt.sh 0
bash topo/set_loss.sh 0
```

Stop the UI relay with `Ctrl+C` in the terminal running `python3 ui/relay.py`.

Remove the routed topology:

```bash
bash topo/teardown.sh
```

## 14. Quick status check

Use this final check while the topology is running:

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}'
docker exec clab-lb-c1 curl -s http://10.0.2.10:9090/healthz
docker exec clab-lb-c1 curl -s http://10.0.2.10:9090/stats
docker exec clab-lb-c1 curl -s 'http://10.0.2.11:8404/;csv'
curl -s http://127.0.0.1:8765/api/status
```

The UI is ready when the topology is visible, `/stats` returns live JSON, HAProxy returns CSV or log data, and `/api/status` reports success.
