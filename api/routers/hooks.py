"""ZLMediaKit web hooks (configured in ZLM config.ini [hook]).

Only non-blocking hooks are enabled by default (stream_changed / not_found / server_started):
if this service is down, ZLM keeps working; nothing is rejected.
"""
import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import settings
from api.db import SessionLocal, get_db
from api.hub import hub
from api.manager import manager
from api.models import Device

log = logging.getLogger("hook")
router = APIRouter(prefix="/index/hook", tags=["zlm-hook"])
OK = {"code": 0, "msg": "success"}


def find_device(db, app, stream):
    return db.scalar(select(Device).where(Device.app == app, Device.stream == stream))


def auto_register(db, app, stream):
    """A stream pushed to an auto app (default "live") becomes a device. Returns None otherwise.
    Called from the hook and from the reconcile loop (hooks can be lost)."""
    if app not in settings.auto_apps or app in (settings.ai_app, settings.ai_ref_app):
        return None
    if db.scalar(select(Device).where(Device.stream == stream)):
        return None     # stream names are unique; same name under another app: leave it alone
    pose = stream in settings.pose_streams
    dev = Device(name=stream, app=app, stream=stream, task="pose" if pose else "detect",
                 det_fps=8 if pose else 5)   # a fall lasts under a second: sample it more often
    db.add(dev)
    db.commit()
    log.info("auto-registered device %s/%s (id=%d)", app, stream, dev.id)
    hub.publish("device", {"device_id": dev.id, "app": app, "stream": stream})
    return dev


@router.post("/on_stream_changed")
def on_stream_changed(body: dict, db: Session = Depends(get_db)):
    app, stream, regist = body.get("app"), body.get("stream"), bool(body.get("regist"))
    if body.get("schema") not in (None, "rtsp") or app in (settings.ai_app, settings.ai_ref_app):
        return OK   # one event per stream; ignore our own annotated outputs (and compare streams)
    dev = find_device(db, app, stream)
    if regist and dev is None:
        dev = auto_register(db, app, stream)
    log.info("stream %s/%s %s", app, stream, "online" if regist else "offline")
    if dev:
        if regist and dev.ai_enabled:
            manager.start(dev)
        elif not regist:
            manager.stop(dev.stream)
        hub.publish("stream", {"device_id": dev.id, "app": app, "stream": stream, "online": regist})
    return OK


@router.post("/on_stream_not_found")
async def on_stream_not_found(body: dict, request: Request):
    """Someone plays a camera stream that is not pulled yet: pull it on demand."""
    with SessionLocal() as db:
        dev = find_device(db, body.get("app"), body.get("stream"))
    if dev and dev.source_url:
        try:
            await request.app.state.zlm.add_proxy(dev.app, dev.stream, dev.source_url)
            log.info("on demand pull %s/%s <- %s", dev.app, dev.stream, dev.source_url)
        except Exception as e:
            log.warning("addStreamProxy failed: %s", e)
    return OK


@router.post("/on_server_started")
async def on_server_started(body: dict, request: Request):
    """ZLM restarted: stream proxies are gone, add them back."""
    from api.main import sync_with_zlm
    await sync_with_zlm(request.app)
    return OK


@router.post("/on_server_keepalive")
def on_server_keepalive(body: dict):
    return OK


@router.post("/on_stream_none_reader")
def on_stream_none_reader(body: dict):
    # Keep streams alive with no viewers: AI must run regardless of who is watching
    return {"code": 0, "close": False}


@router.post("/on_publish")
def on_publish(body: dict):
    """Optional (not enabled by default). Only local workers may publish to the ai/ app."""
    if body.get("app") in (settings.ai_app, settings.ai_ref_app) and body.get("ip") not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return {"code": -1, "msg": "ai app is reserved for local workers"}
    return {"code": 0, "msg": "success", "enable_hls": True, "enable_mp4": False}
