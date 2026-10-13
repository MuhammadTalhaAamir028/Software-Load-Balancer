#!/usr/bin/env bash
# Usage: set_loss.sh <percent>      0 removes the loss.
# Random packet loss with tc netem on R1 eth1 and eth2 egress. Keeps any delay set by set_rtt.sh.
# The percentage is applied in each direction (so a round trip loses a bit more than <percent>).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

[ $# -eq 1 ] && [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "usage: $0 <percent>  (0 = off)"
apply_netem "$(netem_get RTT_MS)" "$1"
