# What `docker kill` of b3 looks like on the routed topology

Measured on 2026-10-15, WSL2 (kernel 6.18.54.1-microsoft-standard-WSL2), containerlab 0.79.0, own LB
(round robin, `config.example.json`), c1 sending 5 requests per second. Raw output: `docker_kill_raw.txt`.
The same test was run twice; both runs gave the same picture (first run: DOWN at +3072 ms, second: +3049 ms).

## Result

| Question | Answer (measured) |
|---|---|
| How does a probe or connect to b3 fail? | **Refused.** TCP RST comes back at once. `ConnectionRefusedError errno=111`, 0.0 to 0.1 ms from the LB container; `curl` from c1 exits with code 7 (about 115 ms including docker exec). |
| Reason in the LB log | `backend_down ... "3 consecutive failures: refused"`. Request errors show `ConnectionRefusedError(111, ...)`. |
| Time from kill to `backend_down` | **3049 ms** (second run), 3072 ms (first run). Three probes at a 1 s interval, so about 3 s is expected. |
| Did any request hang up to `connect_s` (2 s)? | **No.** The slowest request in the 30 s loop took 53 ms. Failed requests returned in 0.3 ms. |
| Client-visible failures | 3 requests got `502` (at +802, +1719, +2632 ms after the kill), one per second, because round robin kept picking b3 until it was marked DOWN. No retry is done, so each failure reaches the client. |
| After DOWN | No request went to b3. Requests were shared between b1, b2 and b4 (33 each). The last answer from b3 was 135 ms before the kill. |
| Re-admission | `start_backend.sh b3` reported healthy at t; `backend_up` (`recovered`) was logged **2034 ms** later (two successful probes at 1 s), and `/stats` showed b3 healthy again. |

## Why "refused" and not "timeout"

`docker kill` stops only the backend process container `clab-lb-b3`. The topology keeps the IP in a
separate "network holder" container (`clab-lb-b3-net`, status stays *Up*), so the address 10.0.3.13
and its network stack stay alive. The kernel there has no listener on port 8000, so it answers the SYN
with a RST. Ping to 10.0.3.13 still works and the neighbour entry on R2 stays REACHABLE.

So in this lab a killed backend is always *refused*, never *unreachable* or *timeout*. A powered-off
host or a cut cable would give `timeout` (up to `timeout_s` = 0.5 s per probe, and requests would wait
`connect_s` = 2 s). The report must say that this experiment measures the refused case. The health
checker's reason vocabulary (`refused`, `reset`, `timeout`, `unreachable`) covers all of them, but only
`refused` was observed.

## Notes for the report

- Detection time of about 3.0 s equals `fail_threshold` x `interval_s`, with the kill landing at a random point in
  the probe cycle (observed 3.05 to 3.07 s). It does not depend on the failure type here because refusal is immediate.
- With a round-robin load of 5 req/s over four backends, about three client requests failed before detection.
  The number scales with request rate and with the share b3 receives.
- The LB log at `INFO` level does not contain individual `health_probe` lines, only the transitions
  `backend_down` and `backend_up`. The per-probe failure sequence is visible in the packet capture instead.
