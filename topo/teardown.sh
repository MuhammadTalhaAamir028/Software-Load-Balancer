#!/usr/bin/env bash
# Stop and remove the whole topology. Safe to run when nothing is deployed.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

containerlab destroy -t "$CLAB_FILE" --cleanup 2>&1 | tail -n 5 || true
rm -f "$NETEM_STATE"
echo "topology removed"
