#!/usr/bin/env bash
# Run the whole experiment plan (brief 11.9) in a random order with a fixed seed.
#   SEED=1 bench/run_all.sh            # run (or resume) everything
#   bench/run_all.sh --list            # print the plan, run nothing
#
# Experiment 1: 3 algorithms x concurrency 50/200/500 x 3 runs x 2 systems = 54 runs, RTT 0
# Experiment 2: round_robin, concurrency 200, RTT 20/100/200 x 3 runs x 2 systems = 18 runs
# Resume: a run whose summary.json says valid:true is skipped. An invalid run is repeated
# up to MAX_ATTEMPTS times; if it still fails it is reported at the end and the plan goes on.
# Needs `ulimit -n 65535` in this shell (hey runs inside the clients, whose limit is checked too).
set -uo pipefail

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$BENCH_DIR/.." && pwd)"
RESULTS_DIR=${RESULTS_DIR:-$REPO_DIR/results}
SEED=${SEED:-1}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-3}

plan() { # prints "exp system algo conc rtt run", shuffled with the seed
  python3 - "$SEED" <<'EOF'
import random, sys
runs = []
for system in ("own", "haproxy"):
    for run in (1, 2, 3):
        for algo in ("round_robin", "least_conn", "ip_hash"):
            for conc in (50, 200, 500):
                runs.append(("exp1", system, algo, conc, 0, run))
        for rtt in (20, 100, 200):
            runs.append(("exp2", system, "round_robin", 200, rtt, run))
random.Random(int(sys.argv[1])).shuffle(runs)
for r in runs:
    print(*r)
EOF
}

if [ "${1:-}" = "--list" ]; then plan; exit 0; fi

[ "$(ulimit -n)" -ge 65535 ] || { echo "error: run 'ulimit -n 65535' first (500 connections)" >&2; exit 1; }
mkdir -p "$RESULTS_DIR"
echo "$SEED" > "$RESULTS_DIR/seed.txt"
for exp in exp1 exp2; do
  mkdir -p "$RESULTS_DIR/$exp"
  [ -f "$RESULTS_DIR/$exp/environment.txt" ] || bash "$BENCH_DIR/environment.sh" > "$RESULTS_DIR/$exp/environment.txt"
done

is_valid() { grep -q '"valid": true' "$1/summary.json" 2>/dev/null; }

failed=()
total=$(plan | wc -l); n=0
while read -r exp system algo conc rtt run; do
  n=$((n + 1))
  dir="$RESULTS_DIR/$exp/${system}_${algo}_c${conc}_rtt${rtt}_run${run}"
  if is_valid "$dir"; then echo "[$n/$total] skip (valid) $dir"; continue; fi
  for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    echo "[$n/$total] $exp $system $algo c=$conc rtt=$rtt run=$run (attempt $attempt)"
    bash "$BENCH_DIR/run_experiment.sh" "$exp" "$system" "$algo" "$conc" "$rtt" "$run" && break
  done
  is_valid "$dir" || failed+=("$dir")
done < <(plan)

echo "done. seed=$SEED, runs not valid after $MAX_ATTEMPTS attempts: ${#failed[@]}"
printf '  %s\n' "${failed[@]}"
[ "${#failed[@]}" -eq 0 ]
