 #!/usr/bin/env bash
# kill_backend.sh <id>
#
# Kills one backend container and records the exact moment (UTC, milliseconds
# since the Unix epoch) just before the kill. The failure-detection time in the
# report is computed as: (timestamp of the first "backend_down" log line) minus
# (the timestamp written by this script), so this number must be precise.
#
# Environment variables:
#   BACKEND_PREFIX  prefix of the container name, default empty.
#                   Compose uses "b3", containerlab uses "clab-lb-b3".
#   KILL_TS_FILE    file that receives the timestamp, default ./kill_ts.txt
#
# Output: the timestamp alone on stdout (so a caller can capture it with
# $(...)); all messages go to stderr. Exit code is non-zero on any failure.

# -e: stop at the first failing command, -u: error on unset variables,
# -o pipefail: a failure inside a pipeline is not hidden by the last command.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <backend-id>   (example: $0 b3)" >&2
  exit 2
fi

id="$1"
# Accept only simple names. This prevents accidental shell or Docker surprises
# such as an empty string or a value that starts with a dash.
if ! [[ "$id" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
  echo "error: invalid backend id '$id'" >&2
  exit 2
fi

# ${VAR:-default} uses the default when VAR is unset or empty.
container="${BACKEND_PREFIX:-}${id}"
ts_file="${KILL_TS_FILE:-./kill_ts.txt}"

# Remove an old timestamp file first. If this script fails below, a missing
# file then clearly marks the run as invalid instead of leaving a stale value.
rm -f "$ts_file"

# The container must exist and be running, otherwise "docker kill" would fail
# and the experiment would silently measure nothing.
# 2>/dev/null hides Docker's own error text; "|| true" keeps set -e from
# aborting here so that the friendlier message below can be printed.
running="$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)"
if [ "$running" != "true" ]; then
  echo "error: container '$container' does not exist or is not running" >&2
  exit 1
fi

# "date -u +%s%3N" prints the UTC time in milliseconds since 1970-01-01.
# %s = whole seconds, %3N = the first three digits of the nanoseconds.
# It is captured IMMEDIATELY before docker kill, so the value can only be
# earlier than the real kill, never later.
kill_ms="$(date -u +%s%3N)"

# Guard against a date implementation without %N support (for example BSD/macOS),
# which would print a malformed number. A valid value has exactly 13 digits.
if ! [[ "$kill_ms" =~ ^[0-9]{13}$ ]]; then
  echo "error: 'date +%s%3N' returned '$kill_ms' (GNU date is required)" >&2
  exit 1
fi

# SIGKILL: the process cannot shut down cleanly, which simulates a real crash.
# The container name that docker prints on success is discarded.
docker kill "$container" >/dev/null

# Persist the value only after the kill succeeded.
printf '%s\n' "$kill_ms" > "$ts_file"

echo "killed $container at $kill_ms (written to $ts_file)" >&2
echo "$kill_ms"