#!/usr/bin/env bash
# Usage: capture.sh <name> <node> <iface> <tcpdump filter> [seconds]
# Example: topo/capture.sh client_lb_http lb eth1 "tcp port 8080" 15
#
# Runs tcpdump in a throw-away container that shares the node's network namespace, so the node image
# does not need tcpdump. Writes captures/<name>.pcap (open in Wireshark, "Save As" pcapng).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

[ $# -ge 4 ] || die "usage: $0 <name> <node> <iface> <tcpdump filter> [seconds]"
name=$1; target=$2; iface=$3; filter=$4; secs=${5:-15}
out="$REPO_DIR/captures"
mkdir -p "$out"

echo "capturing $secs s on $(node "$target") $iface filter: $filter"
docker run --rm --net "container:$(node "$target")" --cap-add NET_RAW --cap-add NET_ADMIN \
  -v "$out:/cap" "$CLIENT_IMAGE" \
  timeout "$secs" tcpdump -n -U -Z root -i "$iface" -w "/cap/$name.pcap" $filter 2>&1 | tail -n 3 || true
ls -l "$out/$name.pcap"
