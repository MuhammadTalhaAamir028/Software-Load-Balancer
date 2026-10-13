#!/usr/bin/env bash
# Print the facts a reader needs to judge an experiment: machine, tool versions, limits.
# Run once per experiment:  bench/environment.sh > results/<exp>/environment.txt
# Needs the topology to be deployed to report container CPU limits.
set -uo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX=${CLAB_PREFIX:-clab-lb-}

echo "date_utc:     $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "git_commit:   $(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null) ($(git -C "$REPO_DIR" branch --show-current 2>/dev/null))"
echo "git_dirty:    $(git -C "$REPO_DIR" status --porcelain 2>/dev/null | wc -l) changed files"
echo "cpu_model:    $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ //')"
echo "cpu_cores:    $(nproc)"
echo "memory:       $(awk '/MemTotal/ {printf "%.1f GiB", $2/1048576}' /proc/meminfo)"
echo "os:           $(. /etc/os-release && echo "$PRETTY_NAME")"
echo "kernel:       $(uname -r)"
echo "docker:       $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
echo "containerlab: $(containerlab version 2>/dev/null | awk '/version/ {print $2; exit}')"
echo "python:       $(python3 --version 2>&1)"
echo "haproxy:      $(docker run --rm haproxy:2.8 haproxy -v 2>/dev/null | head -1)"
echo "hey_sha256:   $(docker exec "${PREFIX}c1" sha256sum /usr/local/bin/hey 2>/dev/null | cut -d' ' -f1)"
echo "wrk:          $(docker exec "${PREFIX}c1" wrk --version 2>&1 | head -1)"
echo "ulimit_host:  $(ulimit -n)"
echo "ulimit_c1:    $(docker exec "${PREFIX}c1" sh -c 'ulimit -n' 2>/dev/null)"
echo "container_cpu_limits (cpus):"
for n in lb haproxy b1 b2 b3 b4 c1 c2 c3 c4; do
  nano=$(docker inspect -f '{{.HostConfig.NanoCpus}}' "${PREFIX}${n}" 2>/dev/null)
  awk -v n="$n" -v v="${nano:-0}" 'BEGIN { printf "  %-8s %s\n", n, (v == 0 ? "unlimited" : v / 1e9) }'
done
echo "backend_delay_ms:"
for n in b1 b2 b3 b4; do
  echo "  $n $(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "${PREFIX}${n}" 2>/dev/null | sed -n 's/^DELAY_MS=//p')"
done
