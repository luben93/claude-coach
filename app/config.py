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

# Optional Strava token file on the volume (alternative to env var).
STRAVA_TOKEN_FILE = DATA_DIR / "strava_token"

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


def strava_token() -> str | None:
    tok = os.environ.get("STRAVA_MCP_TOKEN")
    if tok:
        return tok.strip()
    if STRAVA_TOKEN_FILE.exists():
        return STRAVA_TOKEN_FILE.read_text().strip()
    return None


# --- Strava MCP (remote HTTP/SSE) ------------------------------------------
STRAVA_MCP_URL = os.environ.get("STRAVA_MCP_URL", "")
STRAVA_MCP_TRANSPORT = os.environ.get("STRAVA_MCP_TRANSPORT", "http")


def strava_mcp_config() -> dict | None:
    if not STRAVA_MCP_URL:
        return None
    headers = {}
    tok = strava_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return {"strava": {"type": STRAVA_MCP_TRANSPORT, "url": STRAVA_MCP_URL, "headers": headers}}


# --- Onboarding ------------------------------------------------------------
def is_onboarded() -> bool:
    """The coach has met the athlete once goal.md exists in memory."""
    return (MEMORY_DIR / "goal.md").exists()


# --- Server ----------------------------------------------------------------
HOST = os.environ.get("COACH_HOST", "0.0.0.0")
PORT = int(os.environ.get("COACH_PORT", "8080"))
SYNC_INTERVAL = int(os.environ.get("COACH_SYNC_INTERVAL", str(60 * 60)))
MODEL = os.environ.get("COACH_MODEL", "")
