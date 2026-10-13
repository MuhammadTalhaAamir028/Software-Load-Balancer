# Notes: topology (Talha)

Files: `topo/clab.yml`, `topo/env.sh`, `topo/build.sh`, `topo/routes.sh`, `topo/set_rtt.sh`, `topo/set_loss.sh`, `topo/verify.sh`, `topo/capture.sh`, `topo/teardown.sh`, `topo/frr/*`.

## What it is
Two FRR routers and three subnets, built with containerlab inside WSL2. Everything is a container.

```
 10.0.1.0/24 clients          10.0.2.0/24 LB segment            10.0.3.0/24 backends
 c1 .11  c2 .12              R1 eth2 .1   R2 eth1 .2           b1 .11  b2 .12
 c3 .13  c4 .14              lb .10       haproxy .11          b3 .13  b4 .14
      \                              \     /                          /
   [br-clients]--R1 eth1 .1 ...R1 === [br-lb] === R2 ... R2 eth2 .1--[br-backends]
```

- `br-clients`, `br-lb`, `br-backends` are Linux bridges, each inside its own small "switch" container (`sw-clients`, `sw-lb`, `sw-backends`). A bridge is a software switch: it just forwards Ethernet frames between the ports plugged into it. The PRD asks for a bridge on 10.0.2.0/24 because four things share that segment (R1, R2, LB, HAProxy). The other two subnets need a switch for the same reason: several hosts plus one router port in one subnet.
- Container names are `clab-lb-<node>`. That prefix is what Abdur's `kill_backend.sh` and `start_backend.sh` expect (`BACKEND_PREFIX=clab-lb-`).
- Start with `bash topo/build.sh`, stop with `bash topo/teardown.sh`. Both are safe to run twice.

## Each route line, in plain English
| Where | Route | Meaning |
|---|---|---|
| R1 | `10.0.3.0/24 via 10.0.2.2` | "To reach the backends, hand the packet to R2." R1 has no direct link to that subnet. |
| R2 | `10.0.1.0/24 via 10.0.2.1` | "To reach the clients, hand the packet to R1." Without it, replies would have no way home. |
| c1-c4 | `default via 10.0.1.1` | Anything not on my own subnet goes to R1. |
| b1-b4 | `default via 10.0.3.1` | Anything not on my own subnet goes to R2. |
| lb, haproxy | `10.0.1.0/24 via 10.0.2.1`, `10.0.3.0/24 via 10.0.2.2` | Two different routers answer for two different subnets on the same segment, so the LB needs a specific route for each. |

The routers learn their route through FRR's `staticd` (a static route, no OSPF; decision D5). `ip_forward=1` is what lets a Linux box pass packets between interfaces at all.

`traceroute c1 -> b1` shows `10.0.1.1`, `10.0.2.2`, `10.0.3.11`: it crosses R1 then R2 then arrives. That is the proof the path is routed and not local.

## Network holders (why every restartable node is two containers)
The experiments do `docker kill` on b3 and later `docker start`. A container that is killed loses its network links for good: when it comes back it has no `eth1`, no address and no route. Tested: after one kill/start the backend was unreachable and the next run would have silently used only three backends.

So `b1..b4`, `lb` and `haproxy` are each two containers:
- `<name>-net`: a tiny always-on container that owns the links, the IP address and the routes.
- `<name>`: the real application, sharing that network namespace (`network-mode: container:...`).

`docker kill clab-lb-b3` kills only the backend process. `docker start clab-lb-b3` joins the same network again. Tested twice in a row, the backend answered `/health` with 200 both times and the LB marked it up again.

Side effect for the report: a killed backend now answers a connection attempt with a TCP reset ("connection refused"), instead of staying silent. Both our LB and HAProxy see the same behaviour, so the comparison stays fair. Our health checker records it as `refused`; HAProxy usually logs `Layer4 connection problem` or `Layer4 timeout`.

## What netem does
`tc netem` is a Linux queueing discipline that delays or drops packets on the way out of an interface. `set_rtt.sh <ms>` puts `delay ms/2` on R1 `eth1` (toward the clients) and R1 `eth2` (toward the LB). A request crosses `eth2` once and its reply crosses `eth1` once, so the round trip client to LB gains the full `<ms>`. Setting both interfaces matters because netem only acts on outgoing traffic. `set_loss.sh <pct>` does the same with packet loss and keeps whatever delay is set (the state is remembered in `topo/.netem_state`). `0` removes it. The backend side (R2 to backends) is untouched on purpose: the experiments change the client path only.

Measured: `set_rtt.sh 100` gave ping c1 to LB min/avg/max = 100.55 / 100.61 / 100.64 ms; after `set_rtt.sh 0` it was 0.1 ms.

## Differences from the PRD text (please read)
1. Holder containers, described above. Not in the PRD, needed for `docker kill`/`start`.
2. Three switch containers instead of one bridge, because all three subnets have more than two hosts.
3. The HAProxy node runs as `root`, because `haproxy/render.sh` writes `haproxy.cfg` with mode 0600 and the image's default user (uid 99) cannot read it on Linux. The real fix is one line in `render.sh` (`chmod 644 "$tmp_cfg"` after the `mktemp`); that file is Abdur's.
4. Node images: the backend image is Abdur's `cn-lb-backend` unchanged. The own LB runs in plain `python:3.11-slim` with the repo mounted at `/app`. No derived images are needed.

## One failure case
WSL2 shuts its virtual machine down when no terminal is attached for a while. When it comes back, Docker restarts the containers and all links are gone (clab nodes show `eth0` only). Symptoms: `ping` and `curl` between nodes suddenly give nothing. Fix: run `bash topo/build.sh` again (it redeploys from scratch). To avoid it, keep one WSL terminal open during experiments, or set `vmIdleTimeout=-1` under `[wsl2]` in `%UserProfile%\.wslconfig`.
