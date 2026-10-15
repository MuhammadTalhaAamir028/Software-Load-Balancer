# Screenshot checklist (Talha takes these in Wireshark)

Open each `.pcapng` from `captures/` (Windows path: `\\wsl$\Ubuntu\home\talha\slb\captures`).
Every screenshot must show: the display filter bar, the packet list, and one expanded packet in the details pane.
Frame numbers below are from the committed captures (checked with tshark); if you re-capture, the numbers change but the pattern stays.
Annotate with arrows and short labels. Suggested file names go in `docs/img/`.

## 1. `client_lb_http.pcapng` -> `docs/img/client_lb_http.png`
- Display filter: `ip.addr == 10.0.1.11 && tcp.port == 8080`
- Click: frame 1 (SYN), then frame 4 (`GET /`), then frame 6 or 8 (`200 OK`). Expand Internet Protocol and Transmission Control.
- Annotate: client 10.0.1.11 : ephemeral port -> LB 10.0.2.10 : 8080; SYN / SYN-ACK / ACK (frames 1-3); three `GET /` (frames 4, 10, 14) on the same source port = keep-alive; `Connection: keep-alive` in the response; FIN (frames 19-21).
- Second screenshot, same file, filter `tcp.flags.syn == 1 && tcp.flags.ack == 0`: exactly one row. Annotate "one SYN for three requests".

## 2. `lb_backend_forward.pcapng` -> `docs/img/lb_backend_forward.png`
- Display filter: `ip.src == 10.0.2.10 && ip.dst == 10.0.3.0/24 && http.request && !(http.request.uri == "/health")`
- Click: the single row. Expand Hypertext Transfer Protocol (or right-click > Follow > TCP Stream).
- Annotate: source 10.0.2.10 (LB) -> destination 10.0.3.x : 8000 (backend, here 10.0.3.11); `Host: 10.0.2.10:8080` kept; `X-Forwarded-For: 10.0.1.11` added by the LB; `Connection: close`; `User-Agent: curl/7.81.0` passed through.
- Note for the report: this is a new TCP connection to the backend for the one client request (no pooling).

## 3. `health_probe.pcapng` -> `docs/img/health_probe.png`
- Display filter: `http.request.uri == "/health" || http.response`
- Click: one request and its `200 OK`. Expand HTTP.
- Annotate: LB 10.0.2.10 -> b3 10.0.3.13 : 8000; `GET /health`; `200 OK`; the Time column showing about 1.0 s between probes (use View > Time Display Format > Seconds Since Previous Displayed Packet); each probe is its own connection (six SYNs in 6 s).

## 3b. `health_probe_timeout.pcapng` -> `docs/img/health_probe_timeout.png`
- Display filter: `tcp.flags.syn == 1 || tcp.flags.reset == 1`
- Click: one of the lone SYNs at about 3, 4, 5 s (no reply follows).
- Annotate: SYN from 10.0.2.10 to 10.0.3.13 : 8000 with no SYN-ACK; one SYN per second; no retransmission (probe gives up at 0.5 s); the late SYN-ACK plus RST at the end (after netem was removed).

## 4. `failover.pcapng` -> `docs/img/failover.png` (the most important one)
- Display filter: `ip.addr == 10.0.3.13 && (tcp.flags.syn == 1 || tcp.flags.fin == 1 || tcp.flags.reset == 1)`
- Set View > Time Display Format > Seconds Since Beginning of Capture, and find the first RST. The kill happened about 0.8 s before it.
- Click: first `[RST, ACK]` from 10.0.3.13, expand TCP flags.
- Annotate: before the kill SYN / SYN-ACK / ... / FIN pairs; first RST (b3 killed, "connection refused"); the three failed health probes 1 s apart; the extra refused SYNs that are client requests (these become the `502` answers); after the third probe only one SYN per second remains (probes), no client request to b3. Add the LB log line `backend_down ... 3 consecutive failures: refused` as a text note next to it.
- Second screenshot, filter `http.request.uri == "/"`, to show the last `GET /` to b3 before the kill.

## 5a. `haproxy_client.pcapng` -> `docs/img/haproxy_client.png`
- Display filter: `ip.addr == 10.0.1.11 && tcp.port == 8080`
- Click: SYN, one `GET /`, one `200 OK`.
- Annotate: destination 10.0.2.11 : 8080 (HAProxy, different IP than the own LB); one SYN, three requests; response has no `Connection` header (persistent by default in HTTP/1.1).

## 5b. `haproxy_failover.pcapng` -> `docs/img/haproxy_failover.png`
- Display filter: `ip.addr == 10.0.3.13 && (tcp.flags.syn == 1 || tcp.flags.reset == 1)`
- Click: first `[RST, ACK]` from 10.0.3.13.
- Annotate: HAProxy source 10.0.2.11; same RST pattern as capture 4; HAProxy checks every 1 s (`inter 1000 fall 3 rise 2`); add the HAProxy log line `Server be/b3 is DOWN, reason: Layer4 connection problem ... Connection refused` as a text note. Measured detection 2.73 s after the kill.

## Also needed for the report (not Wireshark)
- Terminal screenshot of `docs/evidence/checkpoint1.txt`: the traceroute block showing `10.0.1.1`, `10.0.2.2`, `10.0.3.11`.
- `docs/architecture.png` (already rendered).
- One screenshot of the dashboard (`http://10.0.2.10:9090/` from c1 or via the port you expose) during a run, if the report mentions it.
