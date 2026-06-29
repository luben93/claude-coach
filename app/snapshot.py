"""Snapshot model + transform.

Pure functions that turn raw Strava activity dicts into the compact JSON the
dashboard reads. Kept separate from fetching so it works no matter how the
activities are pulled (Agent SDK, CLI, or a test fixture).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SNAPSHOT_VERSION = 1


def _km(meters: float | None) -> float | None:
    return round(meters / 1000.0, 2) if meters else None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Strava local times look like "2026-06-28T10:26:16"
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _tag(activity: dict[str, Any]) -> str | None:
    tags = activity.get("activity_tags") or []
    if "Race" in tags:
        return "race"
    if "Workout" in tags:
        return "workout"
    if "Recovery" in tags:
        return "recovery"
    if activity.get("is_commute"):
        return "commute"
    return None


def normalize_ride(a: dict[str, Any]) -> dict[str, Any]:
    """Flatten one Strava activity (list_activities shape) for the dashboard."""
    summary = a.get("summary", a)  # tolerate both nested and flat
    dt = _parse_dt(a.get("start_local") or a.get("start_date_local"))
    return {
        "id": str(a.get("id", "")),
        "name": a.get("name"),
        "date": dt.strftime("%a %b %-d, %H:%M") if dt else None,
        "iso_date": dt.isoformat() if dt else None,
        "distance_km": _km(summary.get("distance")),
        "elevation_m": summary.get("elevation_gain"),
        "avg_hr": summary.get("average_heartrate") or summary.get("avg_hr"),
        "avg_watts": summary.get("average_watts") or summary.get("avg_watts"),
        "sport": a.get("sport_type"),
        "tag": _tag(a),
    }


def build_snapshot(raw_activities: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """Build the full dashboard snapshot from a list of raw activities."""
    now = now or datetime.now(timezone.utc)
    rides = [normalize_ride(a) for a in raw_activities]

    # Rolling-window aggregates. iso_date may be naive (local) — compare on date only.
    cutoff_14d = (now - timedelta(days=14)).date()
    # "this week" = Monday-based ISO week containing `now`
    week_start = (now - timedelta(days=now.weekday())).date()

    km_week = 0.0
    count_14d = 0
    climb_14d = 0.0
    for r in rides:
        d = _parse_dt(r.get("iso_date"))
        if not d:
            continue
        rd = d.date()
        if rd >= cutoff_14d:
            count_14d += 1
            climb_14d += r.get("elevation_m") or 0
        if rd >= week_start:
            km_week += r.get("distance_km") or 0

    return {
        "version": SNAPSHOT_VERSION,
        "synced_at": now.isoformat(),
        "km_this_week": round(km_week, 1),
        "ride_count_14d": count_14d,
        "climb_14d": round(climb_14d),
        "rides": rides[:12],  # dashboard shows the most recent dozen
    }


def write_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    tmp.replace(path)  # atomic so the dashboard never reads a half-written file


def read_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
