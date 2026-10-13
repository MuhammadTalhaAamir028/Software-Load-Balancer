#!/usr/bin/env bash
# Checkpoint 1 evidence: routing tables, traceroute across both routers, ping RTT, one request client -> LB -> backend.
# Prints everything. Redirect to a file if you want to keep it: bash topo/verify.sh | tee results/topo_verify.txt
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

echo "### 1. routing tables"
bash "$TOPO_DIR/routes.sh"

echo; echo "### 2. traceroute c1 -> b1 (expect 10.0.1.1, 10.0.2.2, 10.0.3.11)"
nexec c1 traceroute -n -w 2 -q 1 10.0.3.11

echo; echo "### 3. ping c1 -> LB (RTT should match the last set_rtt.sh value, now: $(netem_get RTT_MS) ms)"
nexec c1 ping -c 5 -i 0.2 "$LB_IP" | tail -n 3

echo; echo "### 4. curl c1 -> LB:$LB_PORT (needs the own LB running with all modules merged)"
nexec c1 curl -s -m 3 -i "http://$LB_IP:$LB_PORT/" || echo "(no answer: is the LB node running? docker logs $(node lb))"
