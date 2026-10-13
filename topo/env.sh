#!/usr/bin/env bash
# Shared settings for every script in topo/. Source it, do not run it.
# Anything that may change lives here. Any value can be overridden from the environment.

TOPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$TOPO_DIR/.." && pwd)"

# ---- lab ----------------------------------------------------------------
LAB=${LAB:-lb}                       # containerlab lab name
CLAB_PREFIX="clab-${LAB}-"           # containers are clab-lb-<node>; same value as BACKEND_PREFIX in backends/*.sh
CLAB_FILE="$TOPO_DIR/clab.yml"

# ---- images -------------------------------------------------------------
FRR_IMAGE=${FRR_IMAGE:-quay.io/frrouting/frr:10.2.1}           # routers and L2 switches
CLIENT_IMAGE=${CLIENT_IMAGE:-cn-lb-client:latest}              # c1-c4 (Abdur: hey, wrk, curl, tcpdump)
BACKEND_IMAGE=${BACKEND_IMAGE:-cn-lb-backend:latest}           # b1-b4 (Abdur)
LB_BASE_IMAGE=${LB_BASE_IMAGE:-python:3.11-slim}               # own LB runs inside this
HAPROXY_BASE_IMAGE=${HAPROXY_BASE_IMAGE:-haproxy:2.8}          # reference LB (version pinned by Abdur)
# The three slim images above have no `ip` command, so containerlab cannot give them an address.
# Dockerfile.node adds iproute2 on top. These are the resulting names.
BACKEND_NET_IMAGE=${BACKEND_NET_IMAGE:-cn-lb-backend-net:latest}
LB_NET_IMAGE=${LB_NET_IMAGE:-cn-lb-lb-net:latest}
HAPROXY_NET_IMAGE=${HAPROXY_NET_IMAGE:-cn-lb-haproxy-net:latest}

# ---- what runs on the LB node ------------------------------------------------
LB_CODE_DIR=${LB_CODE_DIR:-$REPO_DIR}                          # must contain lb/ (core + balancing + obs merged)
LB_CONFIG=${LB_CONFIG:-$REPO_DIR/config.example.json}          # backends 10.0.3.11-.14:8000, listen 8080
HAPROXY_DIR=${HAPROXY_DIR:-$REPO_DIR/haproxy}                  # contains haproxy.cfg (made by haproxy/render.sh)
LB_CPUS=${LB_CPUS:-1.0}                                        # same CPU limit for own LB and HAProxy (fairness)

# ---- backend capacity table (values from backends/.env, Abdur) --------------
B1_DELAY_MS=${B1_DELAY_MS:-5};   B1_CPUS=${B1_CPUS:-1.0}
B2_DELAY_MS=${B2_DELAY_MS:-10};  B2_CPUS=${B2_CPUS:-1.0}
B3_DELAY_MS=${B3_DELAY_MS:-25};  B3_CPUS=${B3_CPUS:-0.5}
B4_DELAY_MS=${B4_DELAY_MS:-50};  B4_CPUS=${B4_CPUS:-0.25}

# ---- addressing (PRD section 6) ----------------------------------------------
NET_CLIENTS=10.0.1.0/24; NET_LB=10.0.2.0/24; NET_BACKENDS=10.0.3.0/24
R1_CLIENT_IP=10.0.1.1;  R1_LB_IP=10.0.2.1     # R1 eth1 / eth2
R2_LB_IP=10.0.2.2;      R2_BACKEND_IP=10.0.3.1 # R2 eth1 / eth2
R1_CLIENT_IF=eth1;      R1_LB_IF=eth2          # netem goes on these two (egress)
LB_IP=10.0.2.10; HAPROXY_IP=10.0.2.11
LB_PORT=8080
CLIENT_NODES="c1 c2 c3 c4"
BACKEND_NODES="b1 b2 b3 b4"

# ---- helpers ------------------------------------------------------------------
# Everything clab.yml expands with ${...} must be exported.
export FRR_IMAGE CLIENT_IMAGE BACKEND_NET_IMAGE LB_NET_IMAGE HAPROXY_NET_IMAGE
export LB_CODE_DIR LB_CONFIG HAPROXY_DIR LB_CPUS
export B1_DELAY_MS B1_CPUS B2_DELAY_MS B2_CPUS B3_DELAY_MS B3_CPUS B4_DELAY_MS B4_CPUS

die() { echo "error: $*" >&2; exit 1; }
node() { echo "${CLAB_PREFIX}$1"; }                 # node name -> container name
nexec() { local n; n=$(node "$1"); shift; docker exec "$n" "$@"; }

NETEM_STATE="$TOPO_DIR/.netem_state"               # remembers the delay and loss currently applied

# apply_netem <total_rtt_ms> <loss_pct>: on R1 eth1 and eth2 egress. Each direction gets half the RTT.
apply_netem() {
  local rtt=$1 loss=$2 half iface
  half=$(awk "BEGIN{print $rtt/2}")
  for iface in "$R1_CLIENT_IF" "$R1_LB_IF"; do
    if awk "BEGIN{exit !($rtt==0 && $loss==0)}"; then
      nexec r1 tc qdisc del dev "$iface" root 2>/dev/null || true
    else
      nexec r1 tc qdisc replace dev "$iface" root netem delay "${half}ms" loss "${loss}%"
    fi
  done
  echo "RTT_MS=$rtt LOSS_PCT=$loss" > "$NETEM_STATE"
  echo "--- r1 qdisc"; for iface in "$R1_CLIENT_IF" "$R1_LB_IF"; do nexec r1 tc qdisc show dev "$iface"; done
}
netem_get() { # netem_get RTT_MS|LOSS_PCT -> current value, 0 if never set
  local v; v=$(grep -o "$1=[0-9.]*" "$NETEM_STATE" 2>/dev/null | cut -d= -f2); echo "${v:-0}"
}
