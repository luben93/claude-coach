"""Wahoo Cloud API — plan upload + workout scheduling.

Two steps to get a workout on the ELEMNT:
  1. POST /v1/plans  → uploads plan JSON to the athlete's Wahoo library, returns plan_id
  2. POST /v1/workouts with plan_id → schedules a workout; appears on the ELEMNT
     app and head unit when the start time is within 6 days from now.

Token: WAHOO_ACCESS_TOKEN env var (no refresh — bearer token only for now).
Local plans are persisted under DATA_DIR/wahoo_plans/ as JSON files.
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger("coach.wahoo")

API_BASE = "https://api.wahooligan.com"
PLANS_DIR = config.DATA_DIR / "wahoo_plans"

WORKOUT_TYPE_INDOOR = 61  # Indoor trainer
WORKOUT_TYPE_ROAD = 15    # Road biking (outdoor)


class WahooError(Exception):
    """Raised on any Wahoo API / auth failure."""


def _token() -> str:
    tok = config.wahoo_access_token()
    if not tok:
        raise WahooError("WAHOO_ACCESS_TOKEN not configured")
    return tok


def _post_form(path: str, fields: dict[str, str]) -> dict[str, Any]:
    url = f"{API_BASE}{path}"
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        log.error("wahoo POST %s failed: HTTP %s %s", path, e.code, detail)
        raise WahooError(f"Wahoo API error (HTTP {e.code}): {detail}") from e
    except urllib.error.URLError as e:
        log.error("wahoo POST %s unreachable: %s", path, e)
        raise WahooError(f"Wahoo unreachable: {e}") from e


def _put_form(path: str, fields: dict[str, str]) -> dict[str, Any]:
    url = f"{API_BASE}{path}"
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=body, method="PUT",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        log.error("wahoo PUT %s failed: HTTP %s %s", path, e.code, detail)
        raise WahooError(f"Wahoo API error (HTTP {e.code}): {detail}") from e
    except urllib.error.URLError as e:
        log.error("wahoo PUT %s unreachable: %s", path, e)
        raise WahooError(f"Wahoo unreachable: {e}") from e


def _encode_plan(plan: dict) -> str:
    return "data:application/json;base64," + base64.b64encode(
        json.dumps(plan).encode()
    ).decode()


# --- Wahoo API calls -------------------------------------------------------

def upload_plan(plan: dict, filename: str, external_id: str) -> dict[str, Any]:
    """Upload a new plan to the athlete's Wahoo library. Returns the Wahoo plan record."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    fields = {
        "plan[file]": _encode_plan(plan),
        "plan[filename]": filename,
        "plan[external_id]": external_id,
        "plan[provider_updated_at]": now_str,
    }
    result = _post_form("/v1/plans", fields)
    log.info("wahoo plan created: id=%s external_id=%s", result.get("id"), external_id)
    return result


def update_plan(wahoo_plan_id: int, plan: dict, filename: str) -> dict[str, Any]:
    """Replace an existing plan's file in Wahoo."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    fields = {
        "plan[file]": _encode_plan(plan),
        "plan[filename]": filename,
        "plan[provider_updated_at]": now_str,
    }
    result = _put_form(f"/v1/plans/{wahoo_plan_id}", fields)
    log.info("wahoo plan updated: id=%s", wahoo_plan_id)
    return result


def schedule_workout(
    wahoo_plan_id: int,
    name: str,
    starts: str,
    minutes: int,
    location: str,
    workout_token: str,
) -> dict[str, Any]:
    """Schedule a workout linked to the plan. Returns the Wahoo workout record."""
    workout_type_id = WORKOUT_TYPE_INDOOR if location == "indoor" else WORKOUT_TYPE_ROAD
    fields = {
        "workout[name]": name,
        "workout[workout_token]": workout_token,
        "workout[workout_type_id]": str(workout_type_id),
        "workout[starts]": starts,
        "workout[minutes]": str(minutes),
        "workout[plan_id]": str(wahoo_plan_id),
    }
    result = _post_form("/v1/workouts", fields)
    log.info(
        "wahoo workout scheduled: id=%s plan_id=%s starts=%s",
        result.get("id"), wahoo_plan_id, starts,
    )
    return result


def update_workout(wahoo_workout_id: int, starts: str, minutes: int) -> dict[str, Any]:
    """Reschedule an existing workout."""
    fields = {
        "workout[starts]": starts,
        "workout[minutes]": str(minutes),
    }
    return _put_form(f"/v1/workouts/{wahoo_workout_id}", fields)


# --- Local persistence -----------------------------------------------------

def _plan_path(external_id: str) -> Path:
    # external_id is already slug-safe (CC-YYYYMMDD-HHMMSS)
    return PLANS_DIR / f"{external_id}.json"


def save_local(external_id: str, meta: dict, plan: dict) -> None:
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    path = _plan_path(external_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"meta": meta, "plan": plan}, indent=2))
    tmp.replace(path)


def load_local(external_id: str) -> dict[str, Any] | None:
    path = _plan_path(external_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def list_local() -> list[dict[str, Any]]:
    if not PLANS_DIR.exists():
        return []
    items = []
    for f in sorted(PLANS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text())
            items.append(data["meta"])
        except (json.JSONDecodeError, OSError, KeyError):
            continue
    return items
