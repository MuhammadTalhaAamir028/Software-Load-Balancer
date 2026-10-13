#!/usr/bin/env bash
# Render and validate the HAProxy config for one of the project's algorithms.
set -euo pipefail

usage() {
  echo "usage: $0 <round_robin|least_conn|ip_hash> [output-config]" >&2
}

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  usage
  exit 2
fi

case "$1" in
  round_robin) haproxy_algo="roundrobin" ;;
  least_conn)  haproxy_algo="leastconn" ;;
  ip_hash)     haproxy_algo="source" ;;
  *)
    echo "error: unsupported algorithm '$1'" >&2
    usage
    exit 2
    ;;
esac

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
output_cfg="${2:-"$script_dir/haproxy.cfg"}"

if ! command -v envsubst >/dev/null 2>&1; then
  echo "error: envsubst is required (install gettext)" >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "error: Docker is required to validate with haproxy:2.8" >&2
  exit 1
fi
if [ ! -d "$(dirname -- "$output_cfg")" ]; then
  echo "error: output directory does not exist: $(dirname -- "$output_cfg")" >&2
  exit 1
fi

tmp_cfg="$(mktemp "${output_cfg}.XXXXXX")"
cleanup() {
  rm -f "$tmp_cfg"
}
trap cleanup EXIT

ALGO="$haproxy_algo" envsubst '${ALGO}' \
  < "$script_dir/haproxy.cfg.tmpl" > "$tmp_cfg"
# The HAProxy image validates as a non-root user, so the bind-mounted config
# must be readable by users other than the file owner.
chmod 644 "$tmp_cfg"

container_cfg="/usr/local/etc/haproxy/haproxy.cfg"
if [ -n "${MSYSTEM:-}" ] && command -v cygpath >/dev/null 2>&1; then
 # Git Bash rewrites Unix-looking Docker arguments as Windows paths.
 host_cfg="$(cygpath -m "$tmp_cfg")"
 mount_spec="type=bind,source=$host_cfg,target=$container_cfg,readonly"
 MSYS_NO_PATHCONV=1 docker run --rm \
   --mount "$mount_spec" \
   haproxy:2.8 \
   haproxy -c -f "$container_cfg"
else
 docker run --rm \
   --mount "type=bind,source=$tmp_cfg,target=$container_cfg,readonly" \
   haproxy:2.8 \
   haproxy -c -f "$container_cfg"
fi

mv "$tmp_cfg" "$output_cfg"
trap - EXIT
echo "validated HAProxy config for $1: $output_cfg"
