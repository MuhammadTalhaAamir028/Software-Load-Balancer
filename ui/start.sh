#!/usr/bin/env bash
# One command for the presentation: make sure the lab is up, then start the UI relay.
#   bash ui/start.sh
# Run it in WSL/Linux from a Linux clone (not /mnt/c). Needs Docker, containerlab, the images
# cn-lb-client and cn-lb-backend (see README "Topology and captures") and, for the packet panel, tshark.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# The own LB reads its config from a file under results/, so the UI can switch the algorithm by
# rewriting that file in place (the same way bench/run_experiment.sh does).
CONFIG_DIR="$REPO_DIR/results/ui"
mkdir -p "$CONFIG_DIR"
python3 bench/make_configs.py --out-dir "$CONFIG_DIR/configs" >/dev/null
[ -f "$CONFIG_DIR/lb_config.json" ] || cp "$CONFIG_DIR/configs/config.round_robin.json" "$CONFIG_DIR/lb_config.json"

mounted="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/etc/lb/config.json"}}{{.Source}}{{end}}{{end}}' clab-lb-lb 2>/dev/null || true)"
if [ "$mounted" != "$CONFIG_DIR/lb_config.json" ]; then
  echo "== (re)deploying the lab with LB_CONFIG=$CONFIG_DIR/lb_config.json"
  LB_CONFIG="$CONFIG_DIR/lb_config.json" bash topo/build.sh
  sleep 8
fi

# HAProxy idles until haproxy.cfg exists. Render it and restart the node.
if [ ! -f haproxy/haproxy.cfg ] || ! docker exec clab-lb-c1 curl -s -m 2 -o /dev/null http://10.0.2.11:8080/ 2>/dev/null; then
  echo "== rendering the HAProxy config and restarting the node"
  bash haproxy/render.sh round_robin >/dev/null
  docker restart -t 1 clab-lb-haproxy >/dev/null
  sleep 3
fi

command -v tshark >/dev/null || echo "note: tshark is not installed, the packet panel will report that"
echo "== UI relay: open http://127.0.0.1:8765/ in your browser"
exec python3 ui/relay.py
