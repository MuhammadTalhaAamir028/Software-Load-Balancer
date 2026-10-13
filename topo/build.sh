#!/usr/bin/env bash
# Build and start the routed topology. Safe to run again: it redeploys from scratch.
# Needs: docker, containerlab (user in the clab_admins group), Abdur's cn-lb-backend and cn-lb-client images.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

command -v docker >/dev/null        || die "docker not found"
command -v containerlab >/dev/null  || die "containerlab not found"
[ -d "$LB_CODE_DIR/lb" ]            || die "LB_CODE_DIR=$LB_CODE_DIR has no lb/ folder"
[ -f "$LB_CONFIG" ]                 || die "LB_CONFIG=$LB_CONFIG not found"

# 1. Images we do not build ourselves.
docker image inspect "$FRR_IMAGE" >/dev/null 2>&1 || { echo "pulling $FRR_IMAGE"; docker pull -q "$FRR_IMAGE" >/dev/null; }
for img in "$CLIENT_IMAGE" "$BACKEND_IMAGE"; do
  docker image inspect "$img" >/dev/null 2>&1 \
    || die "image $img is missing. It belongs to Abdur (see backends/ and bench/client/). Build it or set the variable in topo/env.sh."
done

# 2. Images with `ip` added (cached by Docker, so this is fast after the first time).
build_node() { # build_node <base> <result>
  docker build -q --build-arg BASE="$1" -t "$2" -f "$TOPO_DIR/Dockerfile.node" "$TOPO_DIR" >/dev/null
  echo "built $2 (from $1)"
}
build_node "$BACKEND_IMAGE"     "$BACKEND_NET_IMAGE"
build_node "$LB_BASE_IMAGE"     "$LB_NET_IMAGE"
build_node "$HAPROXY_BASE_IMAGE" "$HAPROXY_NET_IMAGE"

# 3. Deploy. --reconfigure removes an old copy of the lab first.
containerlab deploy -t "$CLAB_FILE" --reconfigure

# 4. Routes (clab.yml only sets addresses).
bash "$TOPO_DIR/routes.sh"
