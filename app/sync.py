"""Background Strava sync.

Pulls recent activities via the agent (which calls the Strava MCP), builds the
dashboard snapshot, and writes it atomically to the mounted volume. Runs on an
interval; also exposed so the server can trigger an immediate sync at startup.
"""
from __future__ import annotations

import asyncio
import logging

from . import config, coach
from .snapshot import build_snapshot, write_snapshot

log = logging.getLogger("coach.sync")


async def run_once() -> bool:
    """Do one sync. Returns True if a fresh snapshot was written."""
    try:
        activities = await coach.fetch_activities(limit=20)
    except Exception as e:
        log.warning("strava fetch failed: %s", e)
        return False
    if not activities:
        log.info("sync: no activities returned (Strava MCP unconfigured or empty)")
        return False
    snap = build_snapshot(activities)
    write_snapshot(config.SNAPSHOT_PATH, snap)
    log.info("sync: wrote snapshot with %d rides", len(snap["rides"]))
    return True


async def loop() -> None:
    """Forever: sync, then sleep SYNC_INTERVAL. First run is immediate."""
    while True:
        try:
            await run_once()
        except Exception as e:  # never let the loop die
            log.exception("sync loop error: %s", e)
        await asyncio.sleep(config.SYNC_INTERVAL)
