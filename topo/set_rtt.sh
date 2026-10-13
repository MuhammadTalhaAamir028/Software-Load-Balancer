#!/usr/bin/env bash
# Usage: set_rtt.sh <ms>      0 removes the delay.
# Adds delay with tc netem on R1 eth1 and eth2 egress (both directions of the client <-> LB path).
# Each direction gets half, so the round trip client <-> LB becomes <ms>. Keeps any loss set by set_loss.sh.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

[ $# -eq 1 ] && [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "usage: $0 <ms>  (0 = off)"
apply_netem "$1" "$(netem_get LOSS_PCT)"
