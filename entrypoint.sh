#!/usr/bin/env bash
# Ensure the writable subdirs exist on the mounted volume before the server
# starts. Runs as the non-root `coach` user. If /data is a host bind-mount, it
# must be writable by uid 10001 (see README — one-time `chown` on the host).
set -e

for d in /data /data/claude-home /data/memory /data/routes; do
  if ! mkdir -p "$d" 2>/dev/null; then
    echo "FATAL: cannot create $d — the mounted ./data is not writable by the" >&2
    echo "       container user (uid 10001). On the host run:" >&2
    echo "         sudo chown -R 10001:10001 ./data" >&2
    exit 1
  fi
done

exec "$@"
