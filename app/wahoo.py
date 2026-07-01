"""Wahoo Cloud API — OAuth + plan upload + workout scheduling.

OAuth flow (mirrors strava.py):
  1. One-time authorization: the athlete visits authorize_url(), approves, and
     Wahoo redirects back to /api/wahoo/callback with a `code`.
  2. exchange_code() trades that code for access + refresh tokens, persisted on
     the volume (wahoo_tokens.json).
  3. Every API call uses a valid access token, auto-refreshing before expiry
     using the stored refresh token.

After OAuth is set up:
  1. POST /v1/plans  → uploads plan JSON to the athlete's Wahoo library
  2. POST /v1/workouts with plan_id → schedules a workout on the ELEMNT

Local plans are persisted under DATA_DIR/wahoo_plans/ as JSON files.
"""
from __future__ import annotations

import base64
import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger("coach.wahoo")

API_BASE   = "https://api.wahooligan.com"
AUTH_URL   = f"{API_BASE}/oauth/authorize"
TOKEN_URL  = f"{API_BASE}/oauth/token"
# offline_data is included so the authorization_code grant reliably returns a
# refresh_token — without long-lived refresh, the access token dies after ~2h
# (the original 401). Over-permission cost is nil for a single-athlete app.
SCOPE      = "workouts_read workouts_write plans_read plans_write offline_data"

TOKENS_PATH = config.DATA_DIR / "wahoo_tokens.json"
PLANS_DIR   = config.DATA_DIR / "wahoo_plans"

WORKOUT_TYPE_INDOOR = 61  # Indoor trainer
WORKOUT_TYPE_ROAD   = 15  # Road biking (outdoor)


class WahooError(Exception):
    """Raised on any Wahoo API / auth failure."""


# --- Token storage ---------------------------------------------------------

def _load_tokens() -> dict[str, Any] | None:
    if not TOKENS_PATH.exists():
        return None
    try:
        return json.loads(TOKENS_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.error("could not read wahoo_tokens.json: %s", e)
        return None


def _save_tokens(tok: dict[str, Any]) -> None:
    TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKENS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(tok))
    tmp.replace(TOKENS_PATH)
    log.info("wahoo tokens saved (expires_at=%s)", tok.get("expires_at"))


def is_connected() -> bool:
    return _load_tokens() is not None


# --- OAuth -----------------------------------------------------------------

def authorize_url(redirect_uri: str) -> str:
    cid = config.wahoo_client_id()
    if not cid:
        raise WahooError("WAHOO_CLIENT_ID not configured")
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _post_token(payload: dict[str, str]) -> dict[str, Any]:
    cid    = config.wahoo_client_id()
    secret = config.wahoo_client_secret()
    if not cid or not secret:
        raise WahooError("WAHOO_CLIENT_ID / WAHOO_CLIENT_SECRET not configured")
    body = {**payload, "client_id": cid, "client_secret": secret}
    data = urllib.parse.urlencode(body).encode()
    req  = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            tok = json.loads(resp.read().decode())
        # Wahoo returns expires_in (seconds); normalize to absolute expires_at
        if "expires_in" in tok and "expires_at" not in tok:
            tok["expires_at"] = int(time.time()) + int(tok["expires_in"])
        return tok
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        log.error("wahoo token endpoint %s failed: HTTP %s %s",
                  payload.get("grant_type"), e.code, detail)
        raise WahooError(f"token request failed (HTTP {e.code})") from e
    except urllib.error.URLError as e:
        log.error("wahoo token endpoint unreachable: %s", e)
        raise WahooError("token endpoint unreachable") from e


def exchange_code(code: str, redirect_uri: str) -> None:
    """One-time: trade an authorization code for tokens and persist them."""
    tok = _post_token({
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": redirect_uri,
    })
    _save_tokens(tok)
    log.info("wahoo connected")


def _refresh(tok: dict[str, Any]) -> dict[str, Any]:
    if not tok.get("refresh_token"):
        # No refresh token was issued at connect time — the access token can't be
        # renewed. Surface it clearly instead of a bare KeyError that would 500.
        raise WahooError("Wahoo issued no refresh_token — reconnect via "
                         "/api/wahoo/connect (offline_data scope required)")
    new    = _post_token({"grant_type": "refresh_token",
                          "refresh_token": tok["refresh_token"]})
    merged = {**tok, **new}
    _save_tokens(merged)
    return merged


def _access_token() -> str:
    tok = _load_tokens()
    if not tok:
        raise WahooError("not connected — authorize Wahoo first (/api/wahoo/connect)")
    # refresh a minute before expiry
    if int(tok.get("expires_at", 0)) <= int(time.time()) + 60:
        log.info("wahoo access token expired, refreshing")
        tok = _refresh(tok)
    return tok["access_token"]


# --- HTTP helpers ----------------------------------------------------------

def _request(method: str, path: str, fields: dict[str, str] | None = None) -> dict[str, Any]:
    url  = f"{API_BASE}{path}"
    body = urllib.parse.urlencode(fields).encode() if fields else None
    req  = urllib.request.Request(
        url, data=body, method=method,
        headers={
            "Authorization":  f"Bearer {_access_token()}",
            "Content-Type":   "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        log.error("wahoo %s %s failed: HTTP %s %s", method, path, e.code, detail)
        raise WahooError(f"Wahoo API error (HTTP {e.code}): {detail}") from e
    except urllib.error.URLError as e:
        log.error("wahoo %s %s unreachable: %s", method, path, e)
        raise WahooError(f"Wahoo unreachable: {e}") from e


def _encode_plan(plan: dict) -> str:
    return "data:application/json;base64," + base64.b64encode(
        json.dumps(plan).encode()
    ).decode()


# --- Wahoo API calls -------------------------------------------------------

def upload_plan(plan: dict, filename: str, external_id: str) -> dict[str, Any]:
    """Upload a new plan to the athlete's Wahoo library. Returns the Wahoo plan record."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    result  = _request("POST", "/v1/plans", {
        "plan[file]":                _encode_plan(plan),
        "plan[filename]":            filename,
        "plan[external_id]":         external_id,
        "plan[provider_updated_at]": now_str,
    })
    log.info("wahoo plan created: id=%s external_id=%s", result.get("id"), external_id)
    return result


def update_plan(wahoo_plan_id: int, plan: dict, filename: str) -> dict[str, Any]:
    """Replace an existing plan's file in Wahoo."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    result  = _request("PUT", f"/v1/plans/{wahoo_plan_id}", {
        "plan[file]":                _encode_plan(plan),
        "plan[filename]":            filename,
        "plan[provider_updated_at]": now_str,
    })
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
    result = _request("POST", "/v1/workouts", {
        "workout[name]":            name,
        "workout[workout_token]":   workout_token,
        "workout[workout_type_id]": str(workout_type_id),
        "workout[starts]":          starts,
        "workout[minutes]":         str(minutes),
        "workout[plan_id]":         str(wahoo_plan_id),
    })
    log.info("wahoo workout scheduled: id=%s plan_id=%s starts=%s",
             result.get("id"), wahoo_plan_id, starts)
    return result


def update_workout(wahoo_workout_id: int, starts: str, minutes: int) -> dict[str, Any]:
    """Reschedule an existing workout."""
    return _request("PUT", f"/v1/workouts/{wahoo_workout_id}", {
        "workout[starts]":   starts,
        "workout[minutes]":  str(minutes),
    })


# --- Local persistence -----------------------------------------------------

def _plan_path(external_id: str) -> Path:
    return PLANS_DIR / f"{external_id}.json"


def save_local(external_id: str, meta: dict, plan: dict) -> None:
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    path = _plan_path(external_id)
    tmp  = path.with_suffix(".tmp")
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
