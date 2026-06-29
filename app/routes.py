"""brouter route generation — geocode + fetch GPX.

Used by the manual route-builder panel on the dashboard. The coach uses the
same underlying script via bash; this is the programmatic path so the UI doesn't
need the agent in the loop for a simple A-to-B route.

GPX files land in DATA_DIR/routes so they persist and are downloadable.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import config

PROFILES = {"trekking", "trekking-fast", "trekking-safe", "fastbike", "moped"}

# Bundled brouter script (copied into the image at /srv/brouter/).
SCRIPT = Path(config.BROUTER_SCRIPT)
ROUTES_DIR = config.DATA_DIR / "routes"


def geocode(place: str) -> tuple[float, float] | None:
    """Place name -> (lon, lat) via Nominatim. Returns None if not found."""
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": place, "format": "json", "limit": "1"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "coach-app-brouter/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    if not data:
        return None
    try:
        return float(data[0]["lon"]), float(data[0]["lat"])
    except (KeyError, ValueError, IndexError):
        return None


_COORD = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def resolve(point: str) -> tuple[float, float] | None:
    """Accept either 'lon,lat' coords or a place name; return (lon, lat)."""
    m = _COORD.match(point)
    if m:
        return float(m.group(1)), float(m.group(2))
    return geocode(point)


def generate(
    start: str,
    end: str,
    *,
    profile: str = "trekking",
    start_label: str = "",
    end_label: str = "",
) -> dict[str, Any]:
    """Generate a GPX route. Returns {ok, file?, error?}."""
    if profile not in PROFILES:
        profile = "trekking"
    s = resolve(start)
    e = resolve(end)
    if not s:
        return {"ok": False, "error": f"could not locate start: {start!r}"}
    if not e:
        return {"ok": False, "error": f"could not locate end: {end!r}"}

    ROUTES_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        "bash", str(SCRIPT),
        "--start", f"{s[0]},{s[1]}",
        "--end", f"{e[0]},{e[1]}",
        "--profile", profile,
        "--origin-label", start_label or start,
        "--dest-label", end_label or end,
        "--output-dir", str(ROUTES_DIR),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "brouter timed out"}
    if out.returncode != 0:
        return {"ok": False, "error": (out.stderr or "route generation failed").strip()}
    path = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    name = Path(path).name if path else ""
    return {"ok": True, "file": name, "profile": profile}


def list_routes() -> list[dict[str, Any]]:
    if not ROUTES_DIR.exists():
        return []
    items = []
    for f in sorted(ROUTES_DIR.glob("*.gpx"), key=lambda p: p.stat().st_mtime, reverse=True):
        items.append({"file": f.name, "size": f.stat().st_size,
                      "modified": int(f.stat().st_mtime)})
    return items
