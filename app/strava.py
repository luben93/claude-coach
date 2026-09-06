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
from datetime import date, datetime, time as _time, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

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


# Strava caps per_page at 200. `max_pages` bounds how far back a filtered scan
# will walk before giving up, so a narrow sport filter over a long date range
# can't turn into an unbounded crawl of the athlete's whole history.
MAX_PER_PAGE = 200
MAX_PAGES = 20


def _to_epoch(value: Any, *, end_of_day: bool = False) -> int | None:
    """Parse a date bound into epoch seconds (UTC).

    Accepts epoch seconds (int or digit string), `YYYY-MM-DD`, or a full ISO-8601
    timestamp. A naive value is read as UTC. A date-only `before` bound becomes
    23:59:59 of that day so a range like 2024-03-01..2024-03-31 includes the 31st.
    Raises ValueError on anything unparseable — callers turn that into a 400.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid date bound: {value!r}")
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    try:
        if len(s) == 10:
            d = date.fromisoformat(s)
            dt = datetime.combine(d, _time.max if end_of_day else _time.min)
        else:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(
            f"invalid date bound {value!r} — use YYYY-MM-DD, an ISO-8601 "
            "timestamp, or epoch seconds"
        ) from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _wanted_sports(sports: str | Iterable[str] | None) -> set[str]:
    """Normalize the sport filter to a set of lowercase terms."""
    if not sports:
        return set()
    if isinstance(sports, str):
        parts: Sequence[str] = sports.split(",")
    else:
        parts = list(sports)
    return {p.strip().lower() for p in parts if p and p.strip()}


def _sport_matches(activity: dict[str, Any], wanted: set[str]) -> bool:
    """Case-insensitive match on Strava's sport_type, substring-friendly.

    Strava's sport types are compound (`GravelRide`, `VirtualRide`, `NordicSki`),
    so a term is a match when it equals the sport OR appears inside it: `ride`
    catches every kind of ride, `ski` every kind of ski, while `gravelride`
    still pins down exactly one.
    """
    sport = (activity.get("sport_type") or activity.get("type") or "").lower()
    if not sport:
        return False
    return any(w == sport or w in sport for w in wanted)


def _normalize(a: dict[str, Any]) -> dict[str, Any]:
    """One raw Strava activity in the shape snapshot.py expects."""
    return {
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
    }


def list_activities(
    limit: int = 20,
    *,
    after: Any = None,
    before: Any = None,
    sports: str | Iterable[str] | None = None,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    """Activity history, newest first, normalized toward snapshot.py's shape.

    Every filter is optional — with none of them this returns the most recent
    `limit` activities, exactly as before. With them it walks back through the
    athlete's history page by page, which is what makes analysing an old race
    possible instead of only the last few weeks:

      after / before  date bounds on the activity start (YYYY-MM-DD, ISO-8601, or
                      epoch seconds). Sent to Strava, so paging starts at the
                      right place rather than scanning forward from today.
      sports          one sport or a comma-separated list / iterable. Strava has
                      no server-side sport filter, so this is applied here and
                      pages are pulled until `limit` matches are found, the
                      history (or the date range) runs out, or `max_pages` pages
                      have been read.

    Raises ValueError for an unparseable or contradictory date bound.
    """
    limit = max(1, int(limit))
    after_ts = _to_epoch(after)
    before_ts = _to_epoch(before, end_of_day=True)
    if after_ts is not None and before_ts is not None and after_ts >= before_ts:
        raise ValueError("`after` must be earlier than `before`")

    wanted = _wanted_sports(sports)
    base: dict[str, Any] = {}
    if after_ts is not None:
        base["after"] = after_ts
    if before_ts is not None:
        base["before"] = before_ts

    # Unfiltered, every activity Strava returns counts, so one page of `limit` is
    # enough. With a sport filter most of a page can be discarded, so pull full
    # pages and keep going until the limit is filled.
    per_page = min(MAX_PER_PAGE, max(limit, 100) if wanted else limit)

    out: list[dict[str, Any]] = []
    page = 1
    while page <= max_pages and len(out) < limit:
        raw = _get("/athlete/activities", {**base, "per_page": per_page, "page": page})
        if not raw:
            break
        for a in raw:
            if wanted and not _sport_matches(a, wanted):
                continue
            out.append(_normalize(a))
            if len(out) >= limit:
                break
        if len(raw) < per_page:
            break  # short page = end of history (or of the date range)
        page += 1
    log.info("strava activities: %d returned (limit=%d after=%s before=%s sports=%s pages=%d)",
             len(out), limit, after_ts, before_ts, sorted(wanted) or "-", page)
    return out


def get_activity(activity_id: int | str) -> dict[str, Any]:
    """DetailedActivity — splits, laps, gear, calories, description, the lot.
    Allowed by the activity:read_all scope we authorize with."""
    return _get(f"/activities/{activity_id}")


# Streams the coach actually reasons about. latlng excluded by default (huge,
# rarely useful for training analysis) but requestable via keys=.
STREAM_DEFAULT_KEYS = ("time,distance,altitude,heartrate,watts,cadence,"
                       "velocity_smooth,temp,moving")


def get_activity_streams(
    activity_id: int | str,
    keys: str | None = None,
    max_points: int = 400,
) -> dict[str, Any]:
    """Full time-series data for one activity, evenly downsampled so the whole
    ride fits in an agent context (a 4h ride at 1Hz is ~14k points per stream).
    Also allowed by activity:read_all — streams are not summary-only."""
    raw = _get(f"/activities/{activity_id}/streams",
               {"keys": keys or STREAM_DEFAULT_KEYS, "key_by_type": "true"})
    out: dict[str, Any] = {}
    for name, s in (raw or {}).items():
        data = s.get("data") or []
        n = len(data)
        if max_points and n > max_points:
            step = -(-n // max_points)  # ceil division
            data = data[::step]
        out[name] = {"data": data, "original_size": n,
                     "downsampled": n > len(data)}
    return out
