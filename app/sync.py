"""Background Strava sync.

Now pulls activities DIRECTLY from the Strava REST API (no agent round-trip),
builds the dashboard snapshot, and writes it atomically to the volume. Runs on
an interval; also triggerable on demand.
"""
from __future__ import annotations

import asyncio
import logging

from . import config, strava
from .snapshot import build_snapshot, write_snapshot

log = logging.getLogger("coach.sync")


async def run_once() -> dict:
    """Do one sync. Returns a status dict (also surfaced by /api/sync)."""
    if not config.strava_configured():
        log.info("sync skipped: Strava client id/secret not configured")
        return {"synced": False, "reason": "strava_not_configured"}
    if not strava.is_connected():
        log.info("sync skipped: Strava not yet authorized (visit /api/strava/connect)")
        return {"synced": False, "reason": "not_connected"}
    try:
        activities = await asyncio.to_thread(strava.list_activities, 20)
    except strava.StravaError as e:
        log.warning("sync: strava fetch failed: %s", e)
        return {"synced": False, "reason": str(e)}
    except Exception as e:  # unexpected — log with stack so it's visible
        log.exception("sync: unexpected error fetching activities")
        return {"synced": False, "reason": f"unexpected: {e}"}

    snap = build_snapshot(activities)
    write_snapshot(config.SNAPSHOT_PATH, snap)
    log.info("sync: wrote snapshot with %d rides", len(snap["rides"]))
    return {"synced": True, "rides": len(snap["rides"])}


async def loop() -> None:
    while True:
        try:
            await run_once()
        except Exception:
            log.exception("sync loop error")
        await asyncio.sleep(config.SYNC_INTERVAL)
