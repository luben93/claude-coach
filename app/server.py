"""FastAPI server — dashboard, chat, Strava (REST/OAuth) sync, brouter routes.

LAN-only by intent; no UI auth. Endpoints:
  GET  /                       -> dashboard + chat page
  GET  /api/health             -> liveness + auth/strava/onboarding status
  GET  /api/onboarding         -> {onboarded: bool}
  GET  /api/snapshot           -> latest cached Strava snapshot
  POST /api/sync               -> trigger an immediate sync
  POST /api/chat               -> SSE stream of the coach's reply
  GET  /api/strava/connect     -> redirect to Strava OAuth (one-time)
  GET  /api/strava/callback    -> OAuth redirect target; stores tokens
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

from . import coach, config, routes, strava, sync, wahoo
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
    log.info("status — sdk=%s claude_auth=%s strava_configured=%s strava_connected=%s onboarded=%s",
             coach.SDK_AVAILABLE, bool(config.claude_oauth_token()),
             config.strava_configured(), strava.is_connected(), config.is_onboarded())
    if config.strava_configured() and not strava.is_connected():
        log.warning("Strava app configured but NOT authorized — visit /api/strava/connect once")
    if not config.strava_configured():
        log.warning("Strava client id/secret not set — no activity data will sync")


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "sdk": coach.SDK_AVAILABLE,
        "authenticated": bool(config.claude_oauth_token()),
        "strava_configured": config.strava_configured(),
        "strava_connected": strava.is_connected(),
        "wahoo_configured": config.wahoo_configured(),
        "onboarded": config.is_onboarded(),
        "snapshot": config.SNAPSHOT_PATH.exists(),
    })


# --- Strava OAuth ----------------------------------------------------------
def _redirect_uri() -> str:
    return config.PUBLIC_BASE_URL.rstrip("/") + "/api/strava/callback"


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


@app.get("/api/onboarding")
async def onboarding() -> JSONResponse:
    return JSONResponse({"onboarded": config.is_onboarded()})


@app.get("/api/snapshot")
async def snapshot() -> JSONResponse:
    snap = read_snapshot(config.SNAPSHOT_PATH)
    if snap is None:
        return JSONResponse({"error": "no snapshot yet"}, status_code=404)
    return JSONResponse(snap)


def _save_last_reply(text: str) -> None:
    try:
        payload = {"text": text, "saved_at": datetime.now(timezone.utc).isoformat()}
        tmp = config.LAST_REPLY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(config.LAST_REPLY_PATH)
    except Exception:
        log.exception("failed to save last reply")


@app.get("/api/last-reply")
async def last_reply() -> JSONResponse:
    if not config.LAST_REPLY_PATH.exists():
        return JSONResponse({"error": "no reply yet"}, status_code=404)
    try:
        return JSONResponse(json.loads(config.LAST_REPLY_PATH.read_text()))
    except Exception:
        return JSONResponse({"error": "unreadable"}, status_code=500)


@app.get("/api/week-plan")
async def week_plan() -> JSONResponse:
    path = config.MEMORY_DIR / "week_plan.md"
    if not path.exists():
        return JSONResponse({"error": "no plan yet"}, status_code=404)
    try:
        text = path.read_text()
        stat = path.stat()
        updated_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        return JSONResponse({"text": text, "updated_at": updated_at})
    except Exception:
        return JSONResponse({"error": "unreadable"}, status_code=500)


@app.post("/api/sync")
async def trigger_sync() -> JSONResponse:
    return JSONResponse(await sync.run_once())


@app.post("/api/chat")
async def chat(req: Request) -> StreamingResponse:
    body = await req.json()
    message = (body.get("message") or "").strip()
    history = body.get("history") or []

    async def event_stream():
        if not message:
            yield _sse({"type": "error", "text": "empty message"})
            yield "data: [DONE]\n\n"
            return
        acc = ""
        try:
            async for chunk in coach.stream_reply(message, history):
                acc += chunk
                yield _sse({"type": "text", "text": chunk})
        except Exception as e:
            yield _sse({"type": "error", "text": str(e)})
        if acc:
            _save_last_reply(acc)
        # onboarding may have completed during this turn; signal the UI to refresh
        yield _sse({"type": "status", "onboarded": config.is_onboarded()})
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/route")
async def make_route(req: Request) -> JSONResponse:
    body = await req.json()
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
    body = await req.json()
    plan = body.get("plan")
    filename = (body.get("filename") or "workout.json").strip()
    scheduled_for = (body.get("scheduled_for") or "").strip()
    duration_minutes = int(body.get("duration_minutes") or 60)
    location = (body.get("location") or "indoor").strip()

    if not plan:
        return JSONResponse({"ok": False, "error": "plan required"}, status_code=400)
    if not scheduled_for:
        return JSONResponse({"ok": False, "error": "scheduled_for required"}, status_code=400)
    if not config.wahoo_configured():
        return JSONResponse(
            {"ok": False, "error": "Wahoo not configured — set WAHOO_ACCESS_TOKEN"},
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


@app.get("/api/wahoo/plans")
async def wahoo_list_plans() -> JSONResponse:
    return JSONResponse({
        "plans": wahoo.list_local(),
        "configured": config.wahoo_configured(),
    })


@app.put("/api/wahoo/push/{external_id}")
async def wahoo_update(external_id: str, req: Request) -> JSONResponse:
    safe = Path(external_id).name  # no traversal
    data = wahoo.load_local(safe)
    if not data:
        return JSONResponse({"ok": False, "error": "plan not found"}, status_code=404)
    if not config.wahoo_configured():
        return JSONResponse(
            {"ok": False, "error": "Wahoo not configured — set WAHOO_ACCESS_TOKEN"},
            status_code=503,
        )
    body = await req.json()
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
