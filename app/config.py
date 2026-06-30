"""Configuration — all from env, defaults tuned for Docker. Generic, no athlete.

Secrets (subscription OAuth token, Strava bearer) live on the mounted volume and
are read at runtime, never hard-coded.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Mounted volume layout (everything that must survive restarts) ---------
DATA_DIR = Path(os.environ.get("COACH_DATA_DIR", "/data"))

# Claude Code home (where `claude setup-token` writes credentials).
CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME_DIR", str(DATA_DIR / "claude-home")))

# The coach's memory — Markdown files it reads AND writes. This is the athlete's
# training journey; it lives on the volume so it grows over time and persists.
MEMORY_DIR = Path(os.environ.get("COACH_MEMORY_DIR", str(DATA_DIR / "memory")))

# Dashboard snapshot (sync job writes, dashboard reads).
SNAPSHOT_PATH = DATA_DIR / "snapshot.json"

# Last coach reply (chat endpoint writes, dashboard plan box reads).
LAST_REPLY_PATH = DATA_DIR / "last_reply.json"

# Bundled brouter script (copied into the image).
BROUTER_SCRIPT = os.environ.get("BROUTER_SCRIPT", "/srv/brouter/fetch_route.sh")


# --- Auth ------------------------------------------------------------------
def claude_oauth_token() -> str | None:
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        return tok.strip()
    f = CLAUDE_HOME / "oauth_token"
    if f.exists():
        return f.read_text().strip()
    return None


# --- Strava REST (OAuth) ---------------------------------------------------
# client id/secret obtain & refresh per-athlete tokens. Read from env only;
# never logged or returned to the UI. We accept a couple of env spellings so a
# hand-written .env doesn't trip people up.
def strava_client_id() -> str | None:
    for k in ("STRAVA_CLIENT_ID", "ClientID", "CLIENT_ID"):
        v = os.environ.get(k)
        if v:
            return v.strip()
    return None


def strava_client_secret() -> str | None:
    for k in ("STRAVA_CLIENT_SECRET", "ClientSecret", "CLIENT_SECRET"):
        v = os.environ.get(k)
        if v:
            return v.strip()
    return None


def strava_configured() -> bool:
    return bool(strava_client_id() and strava_client_secret())


# Public base URL the athlete's browser can reach this app at, for the OAuth
# redirect. Strava requires the callback host to match the app's Authorization
# Callback Domain. Defaults to localhost for local testing.
PUBLIC_BASE_URL = os.environ.get("COACH_PUBLIC_URL", f"http://localhost:{os.environ.get('COACH_PORT','8080')}")


# --- Wahoo -----------------------------------------------------------------
def wahoo_access_token() -> str | None:
    v = os.environ.get("WAHOO_ACCESS_TOKEN", "").strip()
    return v or None


def wahoo_configured() -> bool:
    return bool(wahoo_access_token())


# --- Onboarding ------------------------------------------------------------
def is_onboarded() -> bool:
    """The coach has met the athlete once goal.md exists in memory."""
    return (MEMORY_DIR / "goal.md").exists()


# --- Server ----------------------------------------------------------------
HOST = os.environ.get("COACH_HOST", "0.0.0.0")
PORT = int(os.environ.get("COACH_PORT", "8080"))
SYNC_INTERVAL = int(os.environ.get("COACH_SYNC_INTERVAL", str(60 * 60)))
MODEL = os.environ.get("COACH_MODEL", "")
