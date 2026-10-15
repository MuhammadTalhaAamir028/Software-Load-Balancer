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
bash topo/capture.sh lb_backend_forward lb eth1 "tcp port 8000" 8 &
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

Failure case 2, timeout (the probe waits 0.5 s then gives up). Capture name `health_probe_timeout`:
```bash
bash topo/capture.sh health_probe_timeout lb eth1 "tcp port 8000 and host 10.0.3.13" 10 &
sleep 3
docker exec clab-lb-b3-net tc qdisc add dev eth1 root netem loss 100%   # b3's replies are dropped (egress of b3-net)
sleep 5
docker exec clab-lb-b3-net tc qdisc del dev eth1 root                   # back to normal
wait
```
- Wireshark display filter: `tcp.flags.syn == 1 || tcp.flags.reset == 1` (and `http.request.uri == "/health"` for the good probes)
- Shows: normal probes for about 2 s, then one lone SYN from the LB per second with no SYN-ACK. There are NO TCP retransmissions
  (`tcp.analysis.retransmission` finds nothing): the probe gives up after 0.5 s, before the 1 s TCP SYN retransmit, and the next probe is
  a new connection. The LB logs `backend_down` with reason `3 consecutive failures: timeout`.
- After the netem rule is removed, b3's delayed SYN-ACKs from the abandoned connections arrive and the LB answers each with a RST. That is expected.
- Seen in a test run: 5 unanswered SYNs, one per second, and `backend_down` with reason `timeout` in the LB log.

## 4. `failover` - backend killed
```bash
bash topo/capture.sh failover lb eth1 "host 10.0.3.13" 18 &
sleep 3
# client traffic through the LB, 5 requests per second for 13 s (so some requests are sent to b3)
docker exec clab-lb-c1 bash -c 'end=$(( $(date +%s) + 13 )); while [ $(date +%s) -lt $end ]; do curl -s -m 3 -o /dev/null http://10.0.2.10:8080/; sleep 0.2; done' &
sleep 4
BACKEND_PREFIX=clab-lb- KILL_TS_FILE=/tmp/kill.ts bash backends/kill_backend.sh b3   # kill time in ms
wait
bash backends/start_backend.sh b3           # restore (BACKEND_PREFIX=clab-lb- as above)
```
(For the experiments use Abdur's `backends/kill_backend.sh b3` with `BACKEND_PREFIX=clab-lb-`, it records the kill time to the millisecond.)
- Wireshark display filter: `ip.addr == 10.0.3.13 && (tcp.flags.syn == 1 || tcp.flags.reset == 1 || tcp.flags.fin == 1)`
- Shows: before the kill, each probe is a full handshake (`GET /health`, 200) and each client request is a handshake plus `GET /`. After the kill, every SYN from the LB gets an immediate RST (connection refused) from `10.0.3.13`.
  Until `backend_down` there are two kinds of SYN: the three health probes (1 s apart) and the client requests that round robin still sends to b3 (these become the `502` answers to the client).
  After the third failed probe the LB logs `backend_down` and the only SYNs left are the once-per-second probes (no client request goes to b3 any more).
- Seen in a test run (kill at t=0): failed probes at +1059, +2058 and +3058 ms, `backend_down` at +3060 ms with reason `3 consecutive failures: refused`,
  3 client SYNs refused at +871, +1776 and +2680 ms, then only probes. After the restart `backend_up` with reason `recovered`.
- Note: the backend is a process in a container that keeps its network (see `docs/notes_topo.md`, network holders). A killed process therefore answers with RST, not silence.

## 5. `haproxy_client` and `haproxy_failover` - HAProxy equivalents of 1 and 4
HAProxy is `10.0.2.11`, same ports. Capture inside its network holder `haproxy-net`. Render and start it first:
```bash
bash haproxy/render.sh round_robin     # Abdur's script, writes haproxy/haproxy.cfg
docker restart clab-lb-haproxy         # a node started before haproxy.cfg existed idles with "no haproxy.cfg"; restart it
curl http://10.0.2.11:8080/            # from c1: must answer 200 before you capture
```
Client leg:
```bash
bash topo/capture.sh haproxy_client haproxy-net eth1 "tcp port 8080 and host 10.0.1.11" 8 &
sleep 3
docker exec clab-lb-c1 curl -s -o /dev/null -o /dev/null -o /dev/null http://10.0.2.11:8080/ http://10.0.2.11:8080/ http://10.0.2.11:8080/
wait
```
- Wireshark display filter: `ip.addr == 10.0.1.11 && tcp.port == 8080`
- Shows: one SYN, three `GET /` and three `200` on the same connection. HAProxy's responses carry no `Connection` header (HTTP/1.1 is persistent by default), the own LB sends `Connection: keep-alive`.
Failover (same commands as capture 4, with the curl loop pointed at `http://10.0.2.11:8080/` and capture node `haproxy-net`):
```bash
bash topo/capture.sh haproxy_failover haproxy-net eth1 "host 10.0.3.13" 18 &
# ... client loop and kill_backend.sh exactly as in capture 4 ...
docker logs -t clab-lb-haproxy | grep "b3 is DOWN"
```
- Wireshark display filter: `ip.addr == 10.0.3.13 && (tcp.flags.syn == 1 || tcp.flags.reset == 1)`
- Shows: HAProxy's own health checks (`inter 1000 fall 3 rise 2`) failing the same way (immediate RST). Its log line `Server be/b3 is DOWN, reason: Layer4 connection problem, info: "Connection refused"` is the detection event.
- Seen in a test run (kill at t=0): `Server be/b3 is DOWN` 2.73 s after the kill, three client requests answered `503` (`SC--` in the HAProxy log) before that.

## Screenshots
Take them in Wireshark from the files above. Each screenshot should show the display filter bar, the packet list, and one expanded packet (HTTP headers or TCP flags). Annotate: handshake, request, response, FIN, RST, probe interval.
