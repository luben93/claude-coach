#!/usr/bin/env bash
# End-to-end smoke test against the real Docker image.
#
#   scripts/smoke_test.sh [image]     # default: builds `coach-smoke` from .
#
# Phase 1 runs the image with NO credentials and proves every fault path
# (unconfigured Strava/Wahoo, bad request bodies, upstream 403s surfacing as
# clean 5xx JSON) leaves the server healthy.
# Phase 2 mounts scripts/fake_coach.py over app/coach.py to get a slow,
# deterministic reply, and proves the server-side chat survives client
# disconnects (resume at offset), rejects concurrent sends with 409, and keeps
# history across a container restart.
set -euo pipefail

IMAGE="${1:-coach-smoke}"
PORT="${SMOKE_PORT:-18080}"
BASE="http://127.0.0.1:$PORT"
HERE="$(cd "$(dirname "$0")" && pwd)"
NAME=coach-smoke-run

if [ "${1:-}" = "" ]; then
  echo "== building image =="
  docker build -t "$IMAGE" "$HERE/.."
fi

pass=0; fail=0
ok()  { echo "  PASS $1"; pass=$((pass+1)); }
bad() { echo "  FAIL $1"; fail=$((fail+1)); }
check() { # desc expected actual
  if [ "$2" = "$3" ]; then ok "$1 -> $3"; else bad "$1 -> got $3, want $2"; fi
}
code() { curl -s -o /dev/null -w "%{http_code}" "$@"; }
# cumulative text and last server offset out of an SSE capture
sse_parse() { # field: text | offset
  python3 -c '
import json, sys
field = sys.argv[1]
text, offset = "", 0
for line in sys.stdin:
    if not line.startswith("data: "):
        continue
    try:
        e = json.loads(line[6:])
    except Exception:
        continue
    if isinstance(e, dict) and e.get("type") == "text":
        text += e["text"]
        offset = e.get("offset", offset)
print(text if field == "text" else offset, end="" if field == "text" else "\n")
' "$1"
}
sse_text()   { sse_parse text; }
sse_offset() { sse_parse offset; }

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

wait_up() {
  for _ in $(seq 1 60); do
    curl -fsS "$BASE/api/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "server never came up"; docker logs "$NAME" 2>&1 | tail -50; exit 1
}

echo "== phase 1: real image, no credentials — fault paths =="
cleanup
docker run -d --name "$NAME" -p "$PORT:8080" "$IMAGE" >/dev/null
wait_up
check "GET /api/health" 200 "$(code "$BASE/api/health")"
curl -fsS "$BASE/api/health" | grep -q '"ok": *true' && ok "health ok:true" || bad "health ok:true"
check "GET / (dashboard)" 200 "$(code "$BASE/")"
curl -fsS "$BASE/" | grep -q "Cycling Coach" && ok "dashboard renders" || bad "dashboard renders"
check "GET /api/snapshot before any sync" 404 "$(code "$BASE/api/snapshot")"
check "POST /api/sync without Strava app keys" 200 "$(code -X POST "$BASE/api/sync")"
check "GET /api/strava/activities -> clean 502" 502 "$(code "$BASE/api/strava/activities")"
# history filters are validated before any Strava call, so a bad date is a 400
# even with no credentials, while well-formed filters fall through to the 502
check "GET /api/strava/activities?after=yesterday -> 400" 400 "$(code "$BASE/api/strava/activities?after=yesterday")"
check "GET /api/strava/activities inverted range -> 400" 400 "$(code "$BASE/api/strava/activities?after=2026-08-01&before=2026-07-01")"
check "GET /api/strava/activities filtered -> clean 502" 502 "$(code "$BASE/api/strava/activities?after=2025-02-01&before=2025-03-15&sport=Ride&limit=100")"
check "GET /api/strava/activity/1/streams -> clean 502" 502 "$(code "$BASE/api/strava/activity/1/streams")"
check "GET /api/wahoo/workouts -> clean 503" 503 "$(code "$BASE/api/wahoo/workouts")"
check "POST /api/chat/send malformed body -> 400" 400 "$(code -X POST -H 'Content-Type: application/json' --data 'not json' "$BASE/api/chat/send")"
check "POST /api/chat/send empty message -> 400" 400 "$(code -X POST -H 'Content-Type: application/json' --data '{}' "$BASE/api/chat/send")"
check "POST /api/route missing fields -> 400" 400 "$(code -X POST -H 'Content-Type: application/json' --data '{}' "$BASE/api/route")"

# a real turn: without a token the coach streams its setup guidance — still a
# complete send -> stream -> history round trip through uvicorn + SSE
SEND=$(curl -fsS -X POST -H 'Content-Type: application/json' --data '{"message":"hello"}' "$BASE/api/chat/send")
echo "$SEND" | grep -q '"ok": *true' && ok "chat send accepted" || bad "chat send accepted: $SEND"
TID=$(echo "$SEND" | python3 -c 'import json,sys;print(json.load(sys.stdin)["turn"]["id"])')
STREAM=$(curl -fsS --max-time 30 "$BASE/api/chat/stream?turn_id=$TID&offset=0" || true)
echo "$STREAM" | grep -q '"type": *"done"' && ok "stream completes with done" || bad "stream done event"
sleep 1
curl -fsS "$BASE/api/chat/history" | python3 -c '
import json,sys
d = json.load(sys.stdin)
roles = [m["role"] for m in d["messages"]]
assert roles == ["user", "assistant"], roles
assert d["turn"]["status"] == "done", d["turn"]' \
  && ok "history has user+assistant, turn done" || bad "history after turn"
check "POST /api/chat/clear" 200 "$(code -X POST "$BASE/api/chat/clear")"
check "server healthy after fault-path barrage" 200 "$(code "$BASE/api/health")"

echo "== phase 2: slow fake coach — resume / busy / restart paths =="
cleanup
docker run -d --name "$NAME" -p "$PORT:8080" \
  -v "$HERE/fake_coach.py:/srv/app/coach.py:ro" "$IMAGE" >/dev/null
wait_up
SEND=$(curl -fsS -X POST -H 'Content-Type: application/json' --data '{"message":"stream test"}' "$BASE/api/chat/send")
TID=$(echo "$SEND" | python3 -c 'import json,sys;print(json.load(sys.stdin)["turn"]["id"])')

check "second send while a turn runs -> 409" 409 \
  "$(code -X POST -H 'Content-Type: application/json' --data '{"message":"interrupt"}' "$BASE/api/chat/send")"

# read ~2s of the stream then drop the connection (flaky client)
PART=$(curl -s --max-time 2 "$BASE/api/chat/stream?turn_id=$TID&offset=0" || true)
OFFSET=$(echo "$PART" | sse_offset)
[ "$OFFSET" -gt 0 ] && ok "partial stream before disconnect (offset=$OFFSET)" || bad "partial stream offset=$OFFSET"

# reconnect at the server-reported offset and follow the same turn to the end
REST=$(curl -fsS --max-time 60 "$BASE/api/chat/stream?turn_id=$TID&offset=$OFFSET" || true)
echo "$REST" | grep -q '"type": *"done"' && ok "resumed stream completes" || bad "resumed stream done event"

STITCHED="$(echo "$PART" | sse_text)$(echo "$REST" | sse_text)"
sleep 1
SAVED=$(curl -fsS "$BASE/api/chat/history" | python3 -c 'import json,sys;print(json.load(sys.stdin)["messages"][-1]["content"],end="")')
[ "$STITCHED" = "$SAVED" ] && ok "stitched stream == persisted reply (${#SAVED} chars)" \
  || bad "stitched (${#STITCHED}) != persisted (${#SAVED})"

docker restart "$NAME" >/dev/null
wait_up
GONE=$(curl -fsS --max-time 10 "$BASE/api/chat/stream?turn_id=$TID&offset=0" || true)
echo "$GONE" | grep -q '"type": *"gone"' && ok "stale turn after restart -> gone event" || bad "gone event: $GONE"
curl -fsS "$BASE/api/chat/history" | grep -q "stream test" && ok "history survives restart" || bad "history survives restart"

echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
