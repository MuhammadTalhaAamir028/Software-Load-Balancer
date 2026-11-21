# Live UI

A browser page that shows the running project: topology, routing proof, metrics, failover, HAProxy comparison and a guided demo.
It reads live data from the lab through a small relay. Nothing on the page is sample data: when a source is offline the panel says "No data" and the banner names the source.

## Start (WSL/Linux, from a Linux clone, not /mnt/c)

```bash
bash ui/start.sh
```

Then open <http://127.0.0.1:8765/> in the Windows or WSL browser.

`ui/start.sh` makes sure the lab is deployed with its config under `results/ui/` (so the page can switch the algorithm), renders and restarts HAProxy if it is idle, and starts `ui/relay.py`.
Needs Docker, containerlab, the `cn-lb-client` and `cn-lb-backend` images and, for the packet panel, `tshark`. See the README section "Topology and captures".

## Panels

| Panel | Shows | Source |
|---|---|---|
| U1 Topology | Real wiring (every subnet is a bridge), IPs, up/down, request share per backend, flows | `docker ps`, `/stats` |
| U2 Routing | traceroute c1 to b1 with the hop labelled (R1, R2, b1), routing tables | `docker exec ... ip route`, `traceroute` |
| U3 Network | Configured vs measured RTT and loss, netem state | `topo/.netem_state`, `tc qdisc`, `ping` |
| U4 Request | One live request with its headers explained | `curl -i` from c1 |
| U5 Packets | 6 s capture plus 3 marked requests, labelled: handshake, keep-alive reuse, forwarded request, probes, RST | `topo/capture.sh`, `tshark` |
| U6-U7 Metrics | Requests/s, p50/p95/p99, errors, per-backend share, history charts | `GET :9090/stats` once per second |
| U8 Failover | Kill time, own and HAProxy detection time, failed requests, health events | `logs/lb.jsonl`, `docker logs haproxy` |
| U9 HAProxy | Status, sessions and share per server next to the own LB | `:8404/;csv` |
| U10 Capacity | Delay and CPU per backend (config and live), `/health` timing | `backends/.env`, `docker inspect` |
| U11-U12 Results | Experiment summaries (dry run is labelled), live CPU limits | `results/**/summary.json` |
| U13 Demo | Failover demo and fixed controls | relay controls |
| U14 Freshness | When each source last answered | browser |

## Controls (fixed, no command text from the browser)

Start/stop load, kill/restore b3, RTT 0/20/100/200 ms, own LB and HAProxy algorithm, active system.
The own-LB algorithm switch rewrites the config file the LB container mounts, so the lab must have been started with `bash ui/start.sh`; otherwise the page explains how to redeploy.

## Safety

- The relay binds to `127.0.0.1` only and refuses requests with an unexpected `Host` header.
- Every POST needs the `X-UI-Control` header and a same-origin `Origin`, so another web page cannot trigger a control.
- It runs only the commands written in `relay.py`; values from the browser are checked against fixed lists.
- The page builds its HTML with text nodes only and the relay sends a Content-Security-Policy.

## Files

`relay.py` (relay), `start.sh` (one-command start), `static/` (page), `annotations.json` (the caption under each panel), `../tests/test_ui_relay.py` (unit tests for the parsers, the guard and the packet labelling; run with `python -m unittest tests.test_ui_relay`).
Files the UI creates (`results/ui/`, `captures/ui_live.pcap`) are ignored by git.

## Limits

- HAProxy statistics have no percentiles, so the page shows counters, status and an average response time only.
- "Failed requests" is the growth of the LB error counter between the kill and detection, sampled once per second, so it is approximate.
- The capture decodes every packet; with load running it takes about 15 s.
