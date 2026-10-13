#!/usr/bin/env bash
# start_backend.sh <id>
#
# Starts a stopped backend container and waits until its /health endpoint
# answers 200. Afterwards it prints the UTC millisecond timestamp of the moment
# the backend was confirmed healthy.
#
# Environment variables:
#   BACKEND_PREFIX   prefix of the container name, default empty ("clab-lb-" for containerlab).
#   BACKEND_PORT     port the backend listens on inside its container, default 8000.
#   START_TIMEOUT_S  maximum seconds to wait for /health, default 30.
#
# Output: the timestamp alone on stdout; messages go to stderr.

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <backend-id>   (example: $0 b3)" >&2
  exit 2
fi

id="$1"
if ! [[ "$id" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
  echo "error: invalid backend id '$id'" >&2
  exit 2
fi

container="${BACKEND_PREFIX:-}${id}"
port="${BACKEND_PORT:-8000}"
timeout_s="${START_TIMEOUT_S:-30}"

# Fail early with a clear message when the container does not exist at all.
if ! docker inspect "$container" >/dev/null 2>&1; then
  echo "error: container '$container' does not exist" >&2
  exit 1
fi

# Starting a container that is already running is harmless, so no extra check.
docker start "$container" >/dev/null

# The health probe runs INSIDE the backend container with the Python that is
# already in the image (the image has no curl). This works the same way with
# Compose and with containerlab, and needs no published host port or route.
# Exit code 0 only when /health answers HTTP 200; any error gives non-zero.
probe='import sys, urllib.request
try:
    r = urllib.request.urlopen("http://127.0.0.1:%s/health", timeout=1)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)'
probe="${probe//%s/$port}"

# Poll until the deadline. "date +%s" is whole seconds, enough for a timeout.
deadline=$(( $(date +%s) + timeout_s ))
until docker exec "$container" python -c "$probe" >/dev/null 2>&1; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "error: $container did not answer /health within ${timeout_s}s" >&2
    exit 1
  fi
  sleep 0.2
done

up_ms="$(( $(date -u +%s%N) / 1000000 ))"
if ! [[ "$up_ms" =~ ^[0-9]{13}$ ]]; then
  echo "error: 'date +%s%N' returned '$up_ms' (a date with %N support is required)" >&2
  exit 1
fi

echo "$container is healthy at $up_ms" >&2
echo "$up_ms"