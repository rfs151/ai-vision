"""ai-vision control service.

    uvicorn api.main:app --host 0.0.0.0 --port 8090
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from api.config import settings
from api.db import Base, SessionLocal, engine
from api.hub import hub
from api.manager import manager
from api.models import Device
from api.routers import alarms, devices, hooks, internal
from api.zlm import ZLM

log = logging.getLogger("main")


async def sync_with_zlm(app):
    """Re-add camera proxies and start workers for streams already online
    (hooks may have been missed while this service was down)."""
    zlm = app.state.zlm
    with SessionLocal() as db:
        devs = list(db.scalars(select(Device)))
    for d in devs:
        if d.source_url and d.ai_enabled:
            try:
                await zlm.add_proxy(d.app, d.stream, d.source_url)
            except Exception as e:
                log.info("addStreamProxy %s: %s", d.stream, e)
    try:
        online = {(m["app"], m["stream"]) for m in await zlm.media_list()}
    except Exception as e:
        log.warning("ZLM not reachable at %s: %s", settings.zlm_api, e)
        return
    for d in devs:
        if d.ai_enabled and (d.app, d.stream) in online:
            manager.start(d)
    log.info("sync done: %d devices, %d online streams", len(devs), len(online))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every request URL at INFO; ZLM's secret is a query parameter -> keep it out of logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    Base.metadata.create_all(engine)
    hub.loop = asyncio.get_running_loop()
    app.state.zlm = ZLM()
    if not settings.zlm_secret:
        log.warning("ZLM secret not found: set ZLM_SECRET or ZLM_CONFIG")
    sup = asyncio.create_task(manager.supervise())
    await sync_with_zlm(app)
    yield
    sup.cancel()
    manager.shutdown()
    await app.state.zlm.close()


app = FastAPI(title="ai-vision", version="0.3.0", lifespan=lifespan)
app.include_router(devices.router)
app.include_router(alarms.router)
app.include_router(hooks.router)
app.include_router(internal.router)
app.mount("/media", StaticFiles(directory=settings.data_dir), name="media")


@app.get("/api/workers", tags=["system"])
def workers():
    return manager.all()


@app.get("/api/system", tags=["system"])
async def system(request: Request):
    try:
        await request.app.state.zlm.call("getServerConfig")
        zlm_ok = True
    except Exception:
        zlm_ok = False
    return {"version": app.version, "zlm": {"url": settings.zlm_api, "ok": zlm_ok},
            "workers": len([w for w in manager.all() if w["state"] != "stopped"])}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    """Live events: {"type": "alarm"|"worker"|"stream"|"rules", "data": {...}}"""
    await hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()   # ignore client messages; keeps connection open
    except WebSocketDisconnect:
        hub.disconnect(websocket)
