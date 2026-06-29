#!/usr/bin/env bash
# Fetch a GPX bike route from brouter.de and save it to disk.
# Usage: fetch_route.sh --start LON,LAT --end LON,LAT [--profile PROFILE]
#          [--origin-label TEXT] [--dest-label TEXT] [--output-dir DIR]

set -euo pipefail

START=""
END=""
PROFILE="trekking"
ORIGIN_LABEL=""
DEST_LABEL=""
OUTPUT_DIR="routes"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --start)        START="$2";         shift 2 ;;
    --end)          END="$2";           shift 2 ;;
    --profile)      PROFILE="$2";       shift 2 ;;
    --origin-label) ORIGIN_LABEL="$2";  shift 2 ;;
    --dest-label)   DEST_LABEL="$2";    shift 2 ;;
    --output-dir)   OUTPUT_DIR="$2";    shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$START" || -z "$END" ]]; then
  echo "Error: --start and --end are required (format: lon,lat)" >&2
  exit 1
fi

# Build filename slug
slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/_/g' | sed 's/__*/_/g' | sed 's/^_//;s/_$//'
}

ORIGIN_SLUG=$(slugify "${ORIGIN_LABEL:-$START}")
DEST_SLUG=$(slugify "${DEST_LABEL:-$END}")
FILENAME="bike-route-${ORIGIN_SLUG}-${DEST_SLUG}.gpx"

mkdir -p "$OUTPUT_DIR"
OUTPUT_PATH="${OUTPUT_DIR}/${FILENAME}"

LONLATS="${START}|${END}"
URL="http://brouter.de/brouter?lonlats=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${LONLATS}'))")&profile=${PROFILE}&format=gpx&alternativeidx=0&nogos="

HTTP_STATUS=$(curl -s -o "$OUTPUT_PATH" -w "%{http_code}" \
  -H "User-Agent: claude-brouter-skill/1.0" \
  "$URL")

if [[ "$HTTP_STATUS" != "200" ]]; then
  echo "Error: brouter.de returned HTTP $HTTP_STATUS" >&2
  cat "$OUTPUT_PATH" >&2
  rm -f "$OUTPUT_PATH"
  exit 1
fi

# Verify it looks like a GPX file
if ! grep -q "<gpx" "$OUTPUT_PATH" 2>/dev/null; then
  echo "Error: response does not appear to be a valid GPX file" >&2
  cat "$OUTPUT_PATH" >&2
  rm -f "$OUTPUT_PATH"
  exit 1
fi

echo "$OUTPUT_PATH"
