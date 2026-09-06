"""FastAPI server — dashboard, chat, Strava (REST/OAuth) sync, brouter routes.

LAN-only by intent; no UI auth. Endpoints:
  GET  /                       -> dashboard + chat page
  GET  /api/health             -> liveness + auth/strava/onboarding status
  GET  /api/onboarding         -> {onboarded: bool}
  GET  /api/snapshot           -> latest cached Strava snapshot
  POST /api/sync               -> trigger an immediate sync
  GET  /api/chat/history       -> persisted conversation + active turn state
  POST /api/chat/send          -> start a coach turn (runs server-side)
  GET  /api/chat/stream        -> SSE of a turn, resumable via ?offset=
  POST /api/chat/clear         -> wipe the conversation
  GET  /api/strava/connect     -> redirect to Strava OAuth (one-time)
  GET  /api/strava/callback    -> OAuth redirect target; stores tokens
  GET  /api/strava/activities  -> activity history (with ids); paged, and
                                  filterable by ?after=&before=&sport=
  GET  /api/strava/activity/{id}          -> full activity detail
  GET  /api/strava/activity/{id}/streams  -> downsampled time-series streams
  GET  /api/wahoo/workouts     -> Wahoo workout history (fallback ride data)
  POST /api/route              -> generate a GPX via brouter (manual panel)
  GET  /api/routes             -> list generated GPX files
  GET  /api/routes/{name}      -> download a GPX file
  POST /api/wahoo/push         -> create Wahoo plan + schedule workout on ELEMNT
  GET  /api/wahoo/plans        -> list locally saved Wahoo plans
  PUT  /api/wahoo/push/{id}    -> re-upload plan + reschedule workout
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from . import chat, coach, config, routes, strava, sync, wahoo
from .snapshot import read_snapshot

# Logging: explicit level (override with COACH_LOG_LEVEL), timestamped, named.
# This is what surfaces Strava/coach failures in `docker compose logs`.
import os
logging.basicConfig(
    level=os.environ.get("COACH_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("coach.server")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app = FastAPI(title="Cycling Coach")
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """Any bug in a handler becomes a JSON 500, never a dropped connection —
    one failing endpoint must not look like a dead server to the UI."""
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"error": f"internal error: {exc}"}, status_code=500)


async def _json_body(req: Request) -> dict:
    """Tolerant body parse — malformed/empty JSON becomes {} so endpoints
    answer with their own clean 400s instead of a 500."""
    try:
        body = await req.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


@app.middleware("http")
async def access_log(request: Request, call_next):
    """Log every request with status + latency. API errors become visible here."""
    start = time.monotonic()
    try:
        resp = await call_next(request)
    except Exception:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        raise
    ms = (time.monotonic() - start) * 1000
    # don't spam for the static page / health polling at debug-worthy volume
    level = logging.WARNING if resp.status_code >= 400 else logging.INFO
    if request.url.path == "/" or request.url.path.startswith("/api/snapshot"):
        level = logging.DEBUG
    log.log(level, "%s %s -> %s (%.0fms)", request.method, request.url.path,
            resp.status_code, ms)
    return resp


@app.on_event("startup")
async def _startup() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    asyncio.create_task(sync.loop())
    log.info("coach up — data=%s memory=%s", config.DATA_DIR, config.MEMORY_DIR)
    log.info("public url=%s", config.PUBLIC_BASE_URL)
    log.info("status — sdk=%s claude_auth=%s strava_configured=%s strava_connected=%s wahoo_configured=%s wahoo_connected=%s onboarded=%s",
             coach.SDK_AVAILABLE, bool(config.claude_oauth_token()),
             config.strava_configured(), strava.is_connected(),
             config.wahoo_configured(), wahoo.is_connected(), config.is_onboarded())
    if config.strava_configured() and not strava.is_connected():
        log.warning("Strava app configured but NOT authorized — visit /api/strava/connect once")
    if not config.strava_configured():
        log.warning("Strava client id/secret not set — no activity data will sync")
    if config.wahoo_configured() and not wahoo.is_connected():
        log.warning("Wahoo app configured but NOT authorized — visit /api/wahoo/connect once")


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "sdk": coach.SDK_AVAILABLE,
        "authenticated": bool(config.claude_oauth_token()),
        "strava_configured": config.strava_configured(),
        "strava_connected": strava.is_connected(),
        "wahoo_configured": config.wahoo_configured(),
        "wahoo_connected": wahoo.is_connected(),
        "onboarded": config.is_onboarded(),
        "snapshot": config.SNAPSHOT_PATH.exists(),
    })


# --- OAuth helpers ---------------------------------------------------------
def _redirect_uri() -> str:
    return config.PUBLIC_BASE_URL.rstrip("/") + "/api/strava/callback"


def _wahoo_redirect_uri() -> str:
    return config.PUBLIC_BASE_URL.rstrip("/") + "/api/wahoo/callback"


@app.get("/api/strava/connect")
async def strava_connect() -> RedirectResponse:
    if not config.strava_configured():
        return JSONResponse({"error": "Strava client id/secret not configured"}, status_code=400)
    url = strava.authorize_url(_redirect_uri())
    log.info("redirecting athlete to Strava authorization")
    return RedirectResponse(url)


@app.get("/api/strava/callback")
async def strava_callback(code: str = "", error: str = "") -> RedirectResponse:
    if error:
        log.warning("Strava authorization denied: %s", error)
        return RedirectResponse("/?strava=denied")
    if not code:
        return RedirectResponse("/?strava=missing_code")
    try:
        await asyncio.to_thread(strava.exchange_code, code)
    except strava.StravaError as e:
        log.error("Strava code exchange failed: %s", e)
        return RedirectResponse("/?strava=error")
    # first connect → kick a sync so the dashboard fills immediately
    asyncio.create_task(sync.run_once())
    return RedirectResponse("/?strava=connected")


@app.get("/api/wahoo/connect")
async def wahoo_connect() -> RedirectResponse:
    if not config.wahoo_configured():
        return JSONResponse({"error": "Wahoo client id/secret not configured"}, status_code=400)
    url = wahoo.authorize_url(_wahoo_redirect_uri())
    log.info("redirecting athlete to Wahoo authorization")
    return RedirectResponse(url)


@app.get("/api/wahoo/callback")
async def wahoo_callback(code: str = "", error: str = "") -> RedirectResponse:
    if error:
        log.warning("Wahoo authorization denied: %s", error)
        return RedirectResponse("/?wahoo=denied")
    if not code:
        return RedirectResponse("/?wahoo=missing_code")
    try:
        await asyncio.to_thread(wahoo.exchange_code, code, _wahoo_redirect_uri())
    except wahoo.WahooError as e:
        log.error("Wahoo code exchange failed: %s", e)
        return RedirectResponse("/?wahoo=error")
    return RedirectResponse("/?wahoo=connected")


@app.get("/api/onboarding")
async def onboarding() -> JSONResponse:
    return JSONResponse({"onboarded": config.is_onboarded()})


@app.get("/api/snapshot")
async def snapshot() -> JSONResponse:
    snap = read_snapshot(config.SNAPSHOT_PATH)
    if snap is None:
        return JSONResponse({"error": "no snapshot yet"}, status_code=404)
    return JSONResponse(snap)


@app.get("/api/week-plan")
async def week_plan() -> JSONResponse:
    """The athlete's standing week-ahead plan — the coach's week_plan.md, rendered
    on the dashboard. Distinct from chat: this is the current plan, not the last reply."""
    path = config.WEEK_PLAN_PATH
    if not path.exists():
        return JSONResponse({"error": "no plan yet"}, status_code=404)
    try:
        text = path.read_text()
        updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        return JSONResponse({"text": text, "updated_at": updated_at})
    except Exception:
        return JSONResponse({"error": "unreadable"}, status_code=500)


@app.post("/api/sync")
async def trigger_sync() -> JSONResponse:
    return JSONResponse(await sync.run_once())


# --- chat (server-side turns, resumable SSE) --------------------------------
@app.get("/api/chat/history")
async def chat_history() -> JSONResponse:
    return JSONResponse(chat.state())


@app.post("/api/chat/send")
async def chat_send(req: Request) -> JSONResponse:
    body = await _json_body(req)
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"ok": False, "error": "empty message"}, status_code=400)
    result = await chat.send(message)
    return JSONResponse(result, status_code=200 if result["ok"] else 409)


@app.get("/api/chat/stream")
async def chat_stream(turn_id: str = "", offset: int = 0) -> StreamingResponse:
    async def event_stream():
        try:
            async for evt in chat.stream(turn_id, offset):
                yield _sse(evt)
        except Exception as e:
            # never die silently mid-stream — the client's retry loop handles it
            log.exception("chat stream failed")
            yield _sse({"type": "error", "text": f"stream failed: {e}"})
            yield _sse({"type": "done"})
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat/clear")
async def chat_clear() -> JSONResponse:
    await chat.clear()
    return JSONResponse({"ok": True})


# --- Strava live data (used by the coach agent via curl, and debuggable) ----
@app.get("/api/strava/activities")
async def strava_activities(
    limit: int = 20, after: str = "", before: str = "", sport: str = ""
) -> JSONResponse:
    """Activity history — recent by default, older and filtered on demand.

    With no filters this is the most recent `limit` activities (what the sync and
    the weekly plan use). `after`/`before` (YYYY-MM-DD, ISO-8601 or epoch) and
    `sport` (one sport or a comma-separated list, matched case-insensitively and
    as a substring so `ride` catches GravelRide) page back through the athlete's
    history instead, for analysing an old race or block.
    """
    limit = max(1, min(limit, 200))  # keep a stray ?limit=5000 out of the agent's context
    try:
        acts = await asyncio.to_thread(
            strava.list_activities, limit,
            after=after or None, before=before or None, sports=sport or None,
        )
    except ValueError as e:  # unparseable / inverted date bound — caller's fault
        return JSONResponse({"error": str(e)}, status_code=400)
    except strava.StravaError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({
        "activities": acts,
        "count": len(acts),
        "filters": {"limit": limit, "after": after or None,
                    "before": before or None, "sport": sport or None},
    })


@app.get("/api/strava/activity/{activity_id}")
async def strava_activity(activity_id: str) -> JSONResponse:
    try:
        detail = await asyncio.to_thread(strava.get_activity, activity_id)
    except strava.StravaError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse(detail)


@app.get("/api/strava/activity/{activity_id}/streams")
async def strava_activity_streams(
    activity_id: str, keys: str = "", max_points: int = 400
) -> JSONResponse:
    try:
        streams = await asyncio.to_thread(
            strava.get_activity_streams, activity_id,
            keys or None, max(10, min(max_points, 5000)),
        )
    except strava.StravaError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse(streams)


@app.post("/api/route")
async def make_route(req: Request) -> JSONResponse:
    body = await _json_body(req)
    start = (body.get("start") or "").strip()
    end = (body.get("end") or "").strip()
    profile = (body.get("profile") or "trekking").strip()
    if not start or not end:
        return JSONResponse({"ok": False, "error": "start and end required"}, status_code=400)
    # run blocking brouter call off the event loop
    result = await asyncio.to_thread(
        routes.generate, start, end, profile=profile, start_label=start, end_label=end
    )
    return JSONResponse(result)


@app.get("/api/routes")
async def list_routes() -> JSONResponse:
    return JSONResponse({"routes": routes.list_routes()})


@app.get("/api/routes/{name}")
async def download_route(name: str) -> FileResponse:
    # confine to the routes dir — reject traversal
    safe = Path(name).name
    path = (config.DATA_DIR / "routes" / safe).resolve()
    routes_dir = (config.DATA_DIR / "routes").resolve()
    if routes_dir not in path.parents or not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="application/gpx+xml", filename=safe)


@app.post("/api/wahoo/push")
async def wahoo_push(req: Request) -> JSONResponse:
    body = await _json_body(req)
    plan = body.get("plan")
    filename = (body.get("filename") or "workout.json").strip()
    scheduled_for = (body.get("scheduled_for") or "").strip()
    duration_minutes = int(body.get("duration_minutes") or 60)
    location = (body.get("location") or "indoor").strip()

    if not plan:
        return JSONResponse({"ok": False, "error": "plan required"}, status_code=400)
    if not scheduled_for:
        return JSONResponse({"ok": False, "error": "scheduled_for required"}, status_code=400)
    if not wahoo.is_connected():
        return JSONResponse(
            {"ok": False, "error": "Wahoo not connected — visit /api/wahoo/connect"},
            status_code=503,
        )

    now = datetime.now(timezone.utc)
    external_id = now.strftime("CC-%Y%m%d-%H%M%S")
    plan_name = (plan.get("header") or {}).get("name") or filename.replace(".json", "")

    try:
        wahoo_plan = await asyncio.to_thread(
            wahoo.upload_plan, plan, filename, external_id
        )
        wahoo_plan_id = wahoo_plan["id"]
        wahoo_workout = await asyncio.to_thread(
            wahoo.schedule_workout,
            wahoo_plan_id, plan_name, scheduled_for,
            duration_minutes, location, external_id,
        )
        wahoo_workout_id = wahoo_workout["id"]
    except wahoo.WahooError as e:
        log.warning("wahoo push failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)})

    meta = {
        "external_id": external_id,
        "wahoo_plan_id": wahoo_plan_id,
        "wahoo_workout_id": wahoo_workout_id,
        "filename": filename,
        "name": plan_name,
        "uploaded_at": now.isoformat(),
        "scheduled_for": scheduled_for,
        "duration_minutes": duration_minutes,
        "location": location,
    }
    wahoo.save_local(external_id, meta, plan)
    return JSONResponse({
        "ok": True,
        "external_id": external_id,
        "wahoo_plan_id": wahoo_plan_id,
        "wahoo_workout_id": wahoo_workout_id,
        "name": plan_name,
    })


@app.get("/api/wahoo/workouts")
async def wahoo_workouts(page: int = 1, per_page: int = 30) -> JSONResponse:
    """Wahoo workout history — fallback ride data when Strava is missing detail."""
    if not wahoo.is_connected():
        return JSONResponse(
            {"error": "Wahoo not connected — visit /api/wahoo/connect"},
            status_code=503,
        )
    try:
        data = await asyncio.to_thread(wahoo.list_workouts, page, per_page)
    except wahoo.WahooError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse(data if isinstance(data, dict) else {"workouts": data})


@app.get("/api/wahoo/plans")
async def wahoo_list_plans() -> JSONResponse:
    return JSONResponse({
        "plans": wahoo.list_local(),
        "configured": config.wahoo_configured(),
        "connected": wahoo.is_connected(),
    })


@app.put("/api/wahoo/push/{external_id}")
async def wahoo_update(external_id: str, req: Request) -> JSONResponse:
    safe = Path(external_id).name  # no traversal
    data = wahoo.load_local(safe)
    if not data:
        return JSONResponse({"ok": False, "error": "plan not found"}, status_code=404)
    if not wahoo.is_connected():
        return JSONResponse(
            {"ok": False, "error": "Wahoo not connected — visit /api/wahoo/connect"},
            status_code=503,
        )
    body = await _json_body(req)
    plan = body.get("plan") or data["plan"]
    meta = data["meta"]
    scheduled_for = body.get("scheduled_for") or meta["scheduled_for"]
    duration_minutes = int(body.get("duration_minutes") or meta["duration_minutes"])
    try:
        await asyncio.to_thread(
            wahoo.update_plan, meta["wahoo_plan_id"], plan, meta["filename"]
        )
        await asyncio.to_thread(
            wahoo.update_workout, meta["wahoo_workout_id"], scheduled_for, duration_minutes
        )
    except wahoo.WahooError as e:
        log.warning("wahoo update failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)})

    meta.update({
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "scheduled_for": scheduled_for,
        "duration_minutes": duration_minutes,
    })
    wahoo.save_local(safe, meta, plan)
    return JSONResponse({"ok": True})


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
