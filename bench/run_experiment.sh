#!/usr/bin/env bash
# One experiment run on the deployed topology (brief section 11.4).
#   bench/run_experiment.sh <exp> <own|haproxy> <round_robin|least_conn|ip_hash> <concurrency> <rtt_ms> <run_id>
#
# Needs: topology deployed by topo/build.sh with LB_CONFIG=$BENCH_LB_CONFIG (default
# results/lb_config.json, this script overwrites its contents), clients running cn-lb-client,
# backends running cn-lb-backend, cpu cgroup counters readable on the host.
# Output: results/<exp>/<system>_<algo>_c<N>_rtt<R>_run<id>/ (layout in brief 11.5).
# Exit code: 0 valid run, 3 invalid run (summary.json explains why), other = setup error.
set -euo pipefail

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$BENCH_DIR/../topo/env.sh"            # REPO_DIR, CLAB_PREFIX, node, LB_IP, LB_PORT, ...

usage() { echo "usage: $0 <exp> <own|haproxy> <round_robin|least_conn|ip_hash> <concurrency> <rtt_ms> <run_id>" >&2; exit 2; }
[ $# -eq 6 ] || usage
EXP=$1 SYSTEM=$2 ALGO=$3 CONC=$4 RTT=$5 RUN=$6
case "$SYSTEM" in own|haproxy) ;; *) usage ;; esac
case "$ALGO" in round_robin|least_conn|ip_hash) ;; *) usage ;; esac
[[ "$CONC" =~ ^[0-9]+$ && "$CONC" -ge 4 ]] || die "concurrency must be an integer >= 4"
[[ "$RTT" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "rtt_ms must be a number"

RESULTS_DIR=${RESULTS_DIR:-$REPO_DIR/results}
BENCH_LB_CONFIG=${BENCH_LB_CONFIG:-$RESULTS_DIR/lb_config.json}
LB_LOG=${LB_LOG:-$LB_CODE_DIR/logs/lb.jsonl}      # logging.path in the config is logs/lb.jsonl, cwd /app
HAPROXY_IP=${HAPROXY_IP:-10.0.2.11}
LB_STATS_PORT=${LB_STATS_PORT:-9090}
HAPROXY_STATS_PORT=${HAPROXY_STATS_PORT:-8404}
CLIENT_CPU_LIMIT=${CLIENT_CPU_LIMIT:-1.0}         # cores per client container, used by the validity check
HEY_DURATION=${HEY_DURATION:-60}                  # seconds; 60 for real runs, shorter only for dry runs
KILL_AT_MS=$(( HEY_DURATION * 500 ))              # half way
KILLED_BACKEND=b3
WARMUP_S=5
COOLDOWN_S=10
BACKEND_PREFIX=$CLAB_PREFIX                       # kill_backend.sh / start_backend.sh container prefix
export BACKEND_PREFIX

if [ "$SYSTEM" = own ]; then
  TARGET_NODE=lb;      OTHER_NODE=haproxy; TARGET_IP=$LB_IP;      STATS_URL="http://$LB_IP:$LB_STATS_PORT/stats"; STATS_EXT=json
else
  TARGET_NODE=haproxy; OTHER_NODE=lb;      TARGET_IP=$HAPROXY_IP; STATS_URL="http://$HAPROXY_IP:$HAPROXY_STATS_PORT/;csv"; STATS_EXT=csv
fi
TARGET_URL="http://$TARGET_IP:$LB_PORT/"
RUN_DIR="$RESULTS_DIR/$EXP/${SYSTEM}_${ALGO}_c${CONC}_rtt${RTT}_run${RUN}"

# ---- helpers ----------------------------------------------------------------
wall_ms() { echo $(( $(date -u +%s%N) / 1000000 )); }              # epoch ms, same clock everywhere
mono_ms() { local up _; read -r up _ < /proc/uptime; up=${up/./}; echo $(( 10#$up * 10 )); }  # monotonic ms
client_code() { docker exec "$(node "$1")" curl -s -m 2 -o /dev/null -w '%{http_code}' "$2" 2>/dev/null || true; }
snapshot() { client_code_body "$1" "$STATS_URL" > "$RUN_DIR/stats_$2.$STATS_EXT"; }
client_code_body() { docker exec "$(node "$1")" curl -s -m 3 "$2"; }
wait_until() { # wait_until <seconds> <description> <command...>
  local limit=$1 what=$2; shift 2
  local end=$(( $(mono_ms) + limit * 1000 ))
  until "$@" >/dev/null 2>&1; do
    [ "$(mono_ms)" -lt "$end" ] || die "timed out after ${limit}s waiting for: $what"
    sleep 0.3
  done
}
sleep_until_mono() { while [ "$(mono_ms)" -lt "$1" ]; do sleep 0.02; done; }
target_ok_everywhere() { local c; for c in $CLIENT_NODES; do [ "$(client_code "$c" "$TARGET_URL")" = 200 ] || return 1; done; }
backend_up_in_stats() { # own: /stats says healthy; haproxy: csv status UP
  local body; body=$(client_code_body c1 "$STATS_URL") || return 1
  if [ "$SYSTEM" = own ]; then
    echo "$body" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["per_backend"]["b3"]["healthy"] else 1)'
  else
    echo "$body" | grep -q '^be,b3,.*,UP,'
  fi
}

# ---- lock: two runs at once would ruin both ------------------------------------
mkdir -p "$RESULTS_DIR"
exec 9>"$RESULTS_DIR/.lock"
flock -n 9 || die "another run is in progress ($RESULTS_DIR/.lock)"

cleanup() { # always leave the lab clean: no impairment, no sampler left running
  [ -n "${SAMPLER_PID:-}" ] && kill "$SAMPLER_PID" 2>/dev/null || true
  bash "$TOPO_DIR/set_rtt.sh" 0 >/dev/null 2>&1 || true
}
trap cleanup EXIT

rm -rf "$RUN_DIR"; mkdir -p "$RUN_DIR"
echo "== $EXP $SYSTEM $ALGO c=$CONC rtt=$RTT run=$RUN -> $RUN_DIR"

# ---- 1. reset: all backends up ---------------------------------------------------
for b in $BACKEND_NODES; do bash "$BENCH_DIR/../backends/start_backend.sh" "$b" >/dev/null; done

# ---- 2. stop the other system, (re)start the target with this algorithm -------------
docker stop -t 1 "$(node "$OTHER_NODE")" >/dev/null 2>&1 || true
if [ "$SYSTEM" = own ]; then
  python3 "$BENCH_DIR/make_configs.py" --out-dir "$RESULTS_DIR/configs" >/dev/null
  cat "$RESULTS_DIR/configs/config.$ALGO.json" > "$BENCH_LB_CONFIG"          # in place: the bind mount follows the inode
  docker exec "$(node lb)" cat /etc/lb/config.json | cmp -s - "$BENCH_LB_CONFIG" \
    || die "the LB container does not see $BENCH_LB_CONFIG; deploy with LB_CONFIG=$BENCH_LB_CONFIG"
  mkdir -p "$(dirname "$LB_LOG")"; : >> "$LB_LOG"
else
  bash "$BENCH_DIR/../haproxy/render.sh" "$ALGO" >/dev/null
fi
docker restart -t 1 "$(node "$TARGET_NODE")" >/dev/null
wait_until 40 "$SYSTEM to answer 200 on every client" target_ok_everywhere

# ---- 3. impairment -----------------------------------------------------------------
bash "$TOPO_DIR/set_rtt.sh" "$RTT" > "$RUN_DIR/qdisc.txt" 2>&1
docker exec "$(node c1)" sh -c 'ulimit -n' > "$RUN_DIR/ulimit.txt"

# ---- concurrency split: 50 = 13+13+12+12 -----------------------------------------------
declare -A CONN
i=0
for c in $CLIENT_NODES; do CONN[$c]=$(( CONC / 4 + (i < CONC % 4 ? 1 : 0) )); i=$((i + 1)); done
echo "split: $(for c in $CLIENT_NODES; do printf '%s=%s ' "$c" "${CONN[$c]}"; done)" | tee "$RUN_DIR/split.txt"

# ---- 4. warm-up (discarded) --------------------------------------------------------------
pids=()
for c in $CLIENT_NODES; do
  docker exec "$(node "$c")" hey -z "${WARMUP_S}s" -c "${CONN[$c]}" "$TARGET_URL" >/dev/null 2>&1 & pids+=($!)
done
wait "${pids[@]}" || true
sleep 2
LOG_LINES=$(wc -l < "$LB_LOG" 2>/dev/null || echo 0)   # own LB log: only lines after this belong to the run

# ---- 5. t0 ----------------------------------------------------------------------------------
snapshot c1 s0
python3 "$BENCH_DIR/cpu_sampler.py" --target "$(node "$TARGET_NODE")" --out "$RUN_DIR/cpu.csv" \
  --clients $(for c in $CLIENT_NODES; do node "$c"; done) --clients-out "$RUN_DIR/cpu_clients.csv" &
SAMPLER_PID=$!
T0_MS=$(wall_ms); T0_MONO=$(mono_ms)
echo "$T0_MS" > "$RUN_DIR/t0.txt"
pids=()
for c in $CLIENT_NODES; do
  docker exec "$(node "$c")" hey -z "${HEY_DURATION}s" -c "${CONN[$c]}" -o csv "$TARGET_URL" > "$RUN_DIR/hey_$c.csv" 2>"$RUN_DIR/hey_$c.err" &
  pids+=($!)
done

# ---- 6. t0 + 30 s: snapshot, then kill b3 ---------------------------------------------------
sleep_until_mono $(( T0_MONO + KILL_AT_MS - 100 ))     # leave 100 ms for the snapshot
snapshot c1 s30
KILL_TS_FILE="$RUN_DIR/kill_ts.txt" bash "$BENCH_DIR/../backends/kill_backend.sh" "$KILLED_BACKEND" >/dev/null 2>"$RUN_DIR/kill.err" \
  || echo "kill failed: $(cat "$RUN_DIR/kill.err")" >&2

# ---- 7. t0 + 60 s -----------------------------------------------------------------------------
wait "${pids[@]}" || true
sleep_until_mono $(( T0_MONO + HEY_DURATION * 1000 ))
snapshot c1 s60
kill "$SAMPLER_PID" 2>/dev/null || true; wait "$SAMPLER_PID" 2>/dev/null || true; SAMPLER_PID=

# ---- 8. collect ------------------------------------------------------------------------------------
if [ "$SYSTEM" = own ]; then
  tail -n +$(( LOG_LINES + 1 )) "$LB_LOG" > "$RUN_DIR/lb.jsonl"
else
  docker logs -t --since "$(date -u -d "@$(( T0_MS / 1000 - 5 ))" +%Y-%m-%dT%H:%M:%SZ)" "$(node haproxy)" > "$RUN_DIR/haproxy.log" 2>&1
fi

# ---- 9. restore b3 and wait for the system under test to see it -----------------------------------
bash "$BENCH_DIR/../backends/start_backend.sh" "$KILLED_BACKEND" >/dev/null
wait_until 30 "b3 to be UP again" backend_up_in_stats
sleep "$COOLDOWN_S"
bash "$TOPO_DIR/set_rtt.sh" 0 >/dev/null

# ---- 10. summary and validity -------------------------------------------------------------------------
set +e
python3 "$BENCH_DIR/collect.py" "$RUN_DIR" --exp "$EXP" --system "$SYSTEM" --algorithm "$ALGO" \
  --concurrency "$CONC" --rtt-ms "$RTT" --run "$RUN" --client-cpu-limit "$CLIENT_CPU_LIMIT" --duration "$HEY_DURATION"
exit $?
