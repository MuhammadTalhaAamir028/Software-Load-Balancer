# Captures

One section per capture. All commands run inside WSL2 from the repo root, with the topology up (`bash topo/build.sh`).
`topo/capture.sh <name> <node> <iface> "<tcpdump filter>" [seconds]` starts tcpdump inside a throw-away container that shares the
node's network namespace and writes `captures/<name>.pcap`. Open it in Wireshark and use File > Save As > pcapng
(or `editcap -F pcapng in.pcap out.pcapng`) to get the `.pcapng` the course asks for.
From Windows the folder is `\\wsl$\Ubuntu\home\<you>\slb\captures`.

Both load balancers have one network port (eth1), so one capture on that port sees the client leg and the backend leg together.
Use the Wireshark display filter to separate them.

Tip: the backend only serves `/` and `/health`. Use `/` in demos, other paths return 404.

## 1. `client_lb_http` - client to LB, handshake, request, response, keep-alive
```bash
bash topo/capture.sh client_lb_http lb eth1 "tcp port 8080 and host 10.0.1.11" 10 &
sleep 2
docker exec clab-lb-c1 curl -s -o /dev/null -o /dev/null -o /dev/null \
  http://10.0.2.10:8080/ http://10.0.2.10:8080/ http://10.0.2.10:8080/
wait
```
- tcpdump filter: `tcp port 8080 and host 10.0.1.11`
- Wireshark display filter: `ip.addr == 10.0.1.11 && tcp.port == 8080`. Add `http.request || http.response` to list only the HTTP lines.
- Shows: one SYN / SYN-ACK / ACK, then three `GET /` requests and three responses on the same connection (check with `tcp.flags.syn == 1 && tcp.flags.ack == 0`: exactly one row), then FIN. That is HTTP/1.1 keep-alive between client and LB.
- Seen in a test run: 1 SYN for 3 requests, `Connection: keep-alive` in the responses.

## 2. `lb_backend_forward` - the request the LB sends to a backend
```bash
bash topo/capture.sh lb_backend_forward lb eth1 "tcp port 8000 and not host 10.0.3.13" 8 &
sleep 2
docker exec clab-lb-c1 curl -s -o /dev/null http://10.0.2.10:8080/
wait
```
- tcpdump filter: `tcp port 8000`
- Wireshark display filter: `ip.src == 10.0.2.10 && ip.dst == 10.0.3.0/24 && http.request && !(http.request.uri == "/health")`
- Shows: a NEW TCP connection to the backend for this one request (own LB, decision D2), the head with `X-Forwarded-For: 10.0.1.11` and `Connection: close`, `Host` kept, then the backend reply and the FIN. Click one packet and use Follow TCP Stream.
- Health probes are on the same port; the filter above hides them. They are capture 3.

## 3. `health_probe` - GET /health, success and failure
Success (b3, one probe per second):
```bash
bash topo/capture.sh health_probe lb eth1 "tcp port 8000 and host 10.0.3.13" 6
```
- Wireshark display filter: `http.request.uri == "/health" || http.response`
- Shows: a probe every 1 s from `10.0.2.10`, each its own TCP connection, `HTTP/1.1 200 OK` back, then FIN.

Failure case 1, connection refused: this is capture 4 (the kill).

Failure case 2, timeout (the probe waits 0.5 s then gives up). Run the capture, and in a second terminal:
```bash
docker exec clab-lb-b3-net tc qdisc add dev eth1 root netem loss 100%   # b3 now drops everything
sleep 6
docker exec clab-lb-b3-net tc qdisc del dev eth1 root                   # back to normal
```
- Shows: SYN packets from the LB with no reply, retransmitted (`tcp.analysis.retransmission`). The LB logs the probe as a timeout.

## 4. `failover` - backend killed
```bash
bash topo/capture.sh failover lb eth1 "host 10.0.3.13" 20 &
sleep 4
date -u +%T.%N; docker kill clab-lb-b3     # note this timestamp for the detection-time number
wait
docker start clab-lb-b3                     # restore
```
(For the experiments use Abdur's `backends/kill_backend.sh b3` with `BACKEND_PREFIX=clab-lb-`, it records the kill time to the millisecond.)
- Wireshark display filter: `ip.addr == 10.0.3.13 && (tcp.flags.syn == 1 || tcp.flags.reset == 1 || tcp.flags.fin == 1)`
- Shows: before the kill, each probe is a full handshake and ends with FIN. After the kill, every SYN from the LB gets an immediate RST (connection refused) from `10.0.3.13`, once per second, three times. After the third the LB logs `backend_down`.
- Seen in a test run: `backend_down` event with reason `3 consecutive failures: refused`, then `backend_up` with reason `recovered` after the restart.
- Note: the backend is a process in a container that keeps its network (see `docs/notes_topo.md`, network holders). A killed process therefore answers with RST, not silence.

## 5. `haproxy_client` and `haproxy_failover` - HAProxy equivalents of 1 and 4
HAProxy is `10.0.2.11`, same ports. Capture inside its network holder `haproxy-net`. Render and start it first:
```bash
bash haproxy/render.sh round_robin     # Abdur's script, writes haproxy/haproxy.cfg
bash topo/build.sh                     # the haproxy node starts when haproxy.cfg exists (or: docker restart clab-lb-haproxy)
```
Client leg:
```bash
bash topo/capture.sh haproxy_client haproxy-net eth1 "tcp port 8080 and host 10.0.1.11" 10 &
sleep 2
docker exec clab-lb-c1 curl -s -o /dev/null -o /dev/null http://10.0.2.11:8080/ http://10.0.2.11:8080/
wait
```
- Wireshark display filter: `ip.addr == 10.0.1.11 && tcp.port == 8080`
Failover:
```bash
bash topo/capture.sh haproxy_failover haproxy-net eth1 "host 10.0.3.13" 20 &
sleep 4; date -u +%T.%N; docker kill clab-lb-b3; wait; docker start clab-lb-b3
docker logs -t clab-lb-haproxy | grep "b3 is DOWN"
```
- Wireshark display filter: `ip.addr == 10.0.3.13 && (tcp.flags.syn == 1 || tcp.flags.reset == 1)`
- Shows: HAProxy's own health checks (`inter 1000 fall 3 rise 2`) failing the same way. Its log line `Server be/b3 is DOWN, reason: ...` is the detection event.

## Screenshots
Take them in Wireshark from the files above. Each screenshot should show the display filter bar, the packet list, and one expanded packet (HTTP headers or TCP flags). Annotate: handshake, request, response, FIN, RST, probe interval.
