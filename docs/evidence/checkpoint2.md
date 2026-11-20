# Checkpoint 2: one clean 60 s run with the kill, per system

Run on 2026-10-15 in WSL2 with `bench/run_experiment.sh` (round robin, concurrency 50, RTT 0, 5 s warm-up discarded, `ulimit -n 65535`,
b3 killed with `docker kill` at t0 + 30 s). Files: `results/checkpoint2/checkpoint2/<run>/`. Raw `hey_c*.csv`, `lb.jsonl` and `haproxy.log`
(36 MB in total) stay on the WSL disk at `~/slb/results/checkpoint2/` and are not committed.

**These are dry-run numbers from ONE run each. They are a pipeline check and must not go into the report.** The report uses the 72-run plan.

## Gate result
| Check | own LB | HAProxy |
|---|---|---|
| Script exit code / `valid` | 0 / true | 0 / true |
| `hey_c1..c4.csv` present, `.err` files empty | yes, empty | yes, empty |
| Pre-kill window [0, 30) and post-kill window [30, 60) | 34148 / 38412 requests | 49571 / 46356 requests |
| Failed requests before the kill | 0 | 0 |
| Kill time vs t0 + 30 s | +313 ms (kill_ms - t0_ms - 30000) | +451 ms |
| Detection time (first `backend_down` / `Server be/b3 is DOWN` after the kill) | 2.953 s | 2.642 s |
| Requests failed in the detection window | 996 | 1081 |
| Round robin evenness before the kill (b1/b2/b3/b4) | 8517 / 8517 / 8512 / 8506 | 12407 / 12407 / 12407 / 12408 |
| b3 requests after the kill | 1097 (only before DOWN) | 1223 |
| CPU of the system under test (percent of 1 core, pre / post / peak) | 99.8 / 97.4 / 100.2 | 63.4 / 51.0 / 69.9 |
| Client CPU check | passed (no client above 90 % of its limit for more than 5 s) | passed |

Answers to the questions in the brief: it completed for both systems, the files are clean, both windows exist, no errors. The kill happens a few
hundred ms after t0 + 30 s because `kill_backend.sh` runs after the s30 snapshot; `collect.py` accepts up to 500 ms, both runs are inside it.

## Things to know before the full runs
1. **The own LB is CPU-bound at its 1.0 CPU limit** (99.8 % pre-kill), HAProxy used 63 % of the same limit. At concurrency 50 the own LB is already
   the bottleneck, so Experiment 1 will show a throughput gap that is CPU-limited, not network-limited. Say so in the report.
2. **`bench/environment.sh` prints "unlimited" for every container, which is wrong.** It reads `HostConfig.NanoCpus`; containerlab sets `CpuQuota`
   instead. Real values (docker inspect): lb 1.0, haproxy 1.0, b1 1.0, b2 1.0, b3 0.5, b4 0.25 CPUs (`CpuQuota` 100000 / 50000 / 25000 per 100000 period).
   Clients c1-c4 really have no limit. The environment files under `results/checkpoint2/` carry the wrong "unlimited" lines. Fix for Abdur: read
   `.HostConfig.CpuQuota / .HostConfig.CpuPeriod` as well.
3. **No CPU limit on the clients** (known open item). The client-bottleneck check runs against the nominal `CLIENT_CPU_LIMIT=1.0`; hey used less than 90 % of that.
4. `lb.jsonl` is 13 MB and `haproxy.log` 15 MB for one 60 s run. 72 runs is more than 1 GB of raw logs; plan the disk space.
5. Re-deploy needed: the topology must be built with `LB_CONFIG` pointing at the run's `lb_config.json` (the script checks this). `run_all.sh` uses
   `results/lb_config.json`, so run `LB_CONFIG=$PWD/results/lb_config.json bash topo/build.sh` once before it.
6. Dry-run results live in `results/checkpoint2/` on purpose, not under `results/exp1/`, because `run_all.sh` skips any run whose `summary.json`
   says valid and would otherwise count this dry run as one of the 72.
