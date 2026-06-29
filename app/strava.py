"""Strava REST client with OAuth — replaces the broken MCP auth.

Flow:
  1. One-time authorization: the athlete visits authorize_url(), approves, and
     Strava redirects back to /api/strava/callback with a `code`.
  2. exchange_code() trades that code for an access token + refresh token, which
     we persist on the volume (tokens.json).
  3. Every API call uses a valid access token, auto-refreshing the 6h token with
     the stored refresh token. The client_id/client_secret are read from env and
     used ONLY in the token endpoint calls — never logged, never returned to the UI.

Single athlete: there is exactly one token set on the volume.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger("coach.strava")

AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"
SCOPE = "read,activity:read_all,profile:read_all"

TOKENS_PATH = config.DATA_DIR / "strava_tokens.json"


class StravaError(Exception):
    """Raised on any Strava API/auth failure, with a readable message."""


# --- token storage ---------------------------------------------------------
def _load_tokens() -> dict[str, Any] | None:
    if not TOKENS_PATH.exists():
        return None
    try:
        return json.loads(TOKENS_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.error("could not read strava_tokens.json: %s", e)
        return None


def _save_tokens(tok: dict[str, Any]) -> None:
    TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKENS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(tok))
    tmp.replace(TOKENS_PATH)
    log.info("strava tokens saved (expires_at=%s)", tok.get("expires_at"))


def is_connected() -> bool:
    return _load_tokens() is not None


# --- OAuth -----------------------------------------------------------------
def authorize_url(redirect_uri: str) -> str:
    params = {
        "client_id": config.strava_client_id(),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "approval_prompt": "auto",
        "scope": SCOPE,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _post_token(payload: dict[str, str]) -> dict[str, Any]:
    """POST to the token endpoint. client_secret is in payload but never logged."""
    cid = config.strava_client_id()
    secret = config.strava_client_secret()
    if not cid or not secret:
        raise StravaError("STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET not configured")
    body = {**payload, "client_id": cid, "client_secret": secret}
    data = urllib.parse.urlencode(body).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        # log grant type, NOT the secret
        log.error("strava token endpoint %s failed: HTTP %s %s",
                  payload.get("grant_type"), e.code, detail)
        raise StravaError(f"token request failed (HTTP {e.code})") from e
    except urllib.error.URLError as e:
        log.error("strava token endpoint unreachable: %s", e)
        raise StravaError("token endpoint unreachable") from e


def exchange_code(code: str) -> None:
    """One-time: trade an authorization code for tokens and persist them."""
    tok = _post_token({"grant_type": "authorization_code", "code": code})
    _save_tokens(tok)
    log.info("strava connected for athlete id=%s", (tok.get("athlete") or {}).get("id"))


def _refresh(tok: dict[str, Any]) -> dict[str, Any]:
    new = _post_token({"grant_type": "refresh_token",
                       "refresh_token": tok["refresh_token"]})
    # Strava returns a fresh refresh_token sometimes; keep whichever is newest.
    merged = {**tok, **new}
    _save_tokens(merged)
    return merged


def _access_token() -> str:
    tok = _load_tokens()
    if not tok:
        raise StravaError("not connected — authorize Strava first (/api/strava/connect)")
    # refresh a minute before expiry
    if int(tok.get("expires_at", 0)) <= int(time.time()) + 60:
        log.info("strava access token expired, refreshing")
        tok = _refresh(tok)
    return tok["access_token"]


# --- API -------------------------------------------------------------------
def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    token = _access_token()
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        log.error("strava GET %s failed: HTTP %s %s", path, e.code, detail)
        if e.code == 401:
            raise StravaError("Strava rejected the token (401) — reconnect needed") from e
        raise StravaError(f"Strava API error (HTTP {e.code}) on {path}") from e
    except urllib.error.URLError as e:
        log.error("strava GET %s unreachable: %s", path, e)
        raise StravaError(f"Strava unreachable on {path}") from e


def list_activities(limit: int = 20) -> list[dict[str, Any]]:
    """Recent activities, normalized toward the shape snapshot.py expects."""
    raw = _get("/athlete/activities", {"per_page": limit, "page": 1})
    out = []
    for a in raw:
        out.append({
            "id": a.get("id"),
            "name": a.get("name"),
            "sport_type": a.get("sport_type") or a.get("type"),
            "start_local": a.get("start_date_local"),
            "is_commute": a.get("commute", False),
            "activity_tags": [],  # REST doesn't expose the workout tags the MCP did
            "summary": {
                "distance": a.get("distance"),
                "elevation_gain": a.get("total_elevation_gain"),
                "average_heartrate": a.get("average_heartrate"),
                "average_watts": a.get("average_watts") if a.get("device_watts") else None,
                "moving_time": a.get("moving_time"),
            },
        })
    return out


def get_activity(activity_id: int | str) -> dict[str, Any]:
    return _get(f"/activities/{activity_id}")
