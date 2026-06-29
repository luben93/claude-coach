"""FastAPI server — dashboard, chat, Strava sync, brouter routes.

LAN-only by intent; no UI auth. Endpoints:
  GET  /                    -> dashboard + chat page
  GET  /api/health          -> liveness + auth/MCP/onboarding status
  GET  /api/onboarding      -> {onboarded: bool}
  GET  /api/snapshot        -> latest cached Strava snapshot
  POST /api/sync            -> trigger an immediate sync
  POST /api/chat            -> SSE stream of the coach's reply
  POST /api/route           -> generate a GPX via brouter (manual panel)
  GET  /api/routes          -> list generated GPX files
  GET  /api/routes/{name}   -> download a GPX file
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import coach, config, routes, sync
from .snapshot import read_snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("coach.server")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app = FastAPI(title="Cycling Coach")


@app.on_event("startup")
async def _startup() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    asyncio.create_task(sync.loop())
    log.info("coach up — data=%s memory=%s strava=%s onboarded=%s",
             config.DATA_DIR, config.MEMORY_DIR,
             "configured" if config.strava_mcp_config() else "OFF",
             config.is_onboarded())


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "sdk": coach.SDK_AVAILABLE,
        "authenticated": bool(config.claude_oauth_token()),
        "strava_mcp": bool(config.strava_mcp_config()),
        "onboarded": config.is_onboarded(),
        "snapshot": config.SNAPSHOT_PATH.exists(),
    })


@app.get("/api/onboarding")
async def onboarding() -> JSONResponse:
    return JSONResponse({"onboarded": config.is_onboarded()})


@app.get("/api/snapshot")
async def snapshot() -> JSONResponse:
    snap = read_snapshot(config.SNAPSHOT_PATH)
    if snap is None:
        return JSONResponse({"error": "no snapshot yet"}, status_code=404)
    return JSONResponse(snap)


@app.post("/api/sync")
async def trigger_sync() -> JSONResponse:
    wrote = await sync.run_once()
    return JSONResponse({"synced": wrote})


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
        try:
            async for chunk in coach.stream_reply(message, history):
                yield _sse({"type": "text", "text": chunk})
        except Exception as e:
            yield _sse({"type": "error", "text": str(e)})
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


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
