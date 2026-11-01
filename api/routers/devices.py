import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import settings
from api.db import get_db
from api.hub import hub
from api.manager import manager
from api.models import Device
from api.schemas import DeviceIn, DeviceOut, DeviceUpdate, Rules
from api.zlm import play_urls

log = logging.getLogger("devices")
router = APIRouter(prefix="/api/devices", tags=["devices"])


async def online_set(request: Request):
    try:
        return {(m["app"], m["stream"]) for m in await request.app.state.zlm.media_list()}
    except Exception as e:
        log.warning("ZLM getMediaList failed: %s", e)
        return set()


def to_out(dev, online, where):
    out = DeviceOut.model_validate(dev)
    out.online = (dev.app, dev.stream) in online
    out.worker = manager.status(dev.stream)
    out.play = {"source": play_urls(dev.app, dev.stream, **where),
                "ai": play_urls(settings.ai_app, dev.stream, **where)}
    return out


def host_of(request):
    """Where the browser reached us. Behind the gateway (X-Forwarded-Proto set by Caddy) the play
    URLs use the same origin: an https page can't load http video (mixed content)."""
    proto = request.headers.get("x-forwarded-proto")
    if proto:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        return {"host": host.split(":")[0], "origin": f"{proto}://{host}"}
    return {"host": request.url.hostname or "127.0.0.1"}


def get_or_404(db, device_id):
    dev = db.get(Device, device_id)
    if not dev:
        raise HTTPException(404, "device not found")
    return dev


@router.get("", response_model=list[DeviceOut])
async def list_devices(request: Request, db: Session = Depends(get_db)):
    online = await online_set(request)
    return [to_out(d, online, host_of(request)) for d in db.scalars(select(Device).order_by(Device.id))]


@router.post("", response_model=DeviceOut, status_code=201)
async def create_device(body: DeviceIn, request: Request, db: Session = Depends(get_db)):
    if db.scalar(select(Device).where(Device.stream == body.stream)):
        raise HTTPException(409, "stream already exists")
    dev = Device(**body.model_dump())
    db.add(dev)
    db.commit()
    if dev.source_url:
        try:
            await request.app.state.zlm.add_proxy(dev.app, dev.stream, dev.source_url)
        except Exception as e:
            log.warning("addStreamProxy failed: %s", e)
    online = await online_set(request)
    if (dev.app, dev.stream) in online and dev.ai_enabled:
        manager.start(dev)
    return to_out(dev, online, host_of(request))


@router.get("/{device_id}", response_model=DeviceOut)
async def get_device(device_id: int, request: Request, db: Session = Depends(get_db)):
    return to_out(get_or_404(db, device_id), await online_set(request), host_of(request))


@router.patch("/{device_id}", response_model=DeviceOut)
async def update_device(device_id: int, body: DeviceUpdate, request: Request, db: Session = Depends(get_db)):
    dev = get_or_404(db, device_id)
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(dev, k, v)
    db.commit()
    zlm = request.app.state.zlm
    if "source_url" in changes:
        await zlm.del_proxy(dev.app, dev.stream)
        if dev.source_url:
            await zlm.add_proxy(dev.app, dev.stream, dev.source_url)
    online = await online_set(request)
    is_online = (dev.app, dev.stream) in online
    if not dev.ai_enabled:
        manager.stop(dev.stream)
    elif is_online and changes.keys() & {"ai_enabled", "det_fps", "classes"}:
        manager.restart(dev)   # worker args changed
    return to_out(dev, online, host_of(request))


@router.delete("/{device_id}", status_code=204)
async def delete_device(device_id: int, request: Request, db: Session = Depends(get_db)):
    dev = get_or_404(db, device_id)
    manager.stop(dev.stream, forget=True)
    if dev.source_url:
        await request.app.state.zlm.del_proxy(dev.app, dev.stream)
    db.delete(dev)
    db.commit()


@router.get("/{device_id}/rules", response_model=Rules)
def get_rules(device_id: int, db: Session = Depends(get_db)):
    return get_or_404(db, device_id).rules


@router.put("/{device_id}/rules", response_model=Rules)
def put_rules(device_id: int, rules: Rules, db: Session = Depends(get_db)):
    """Workers poll the version and hot-reload; no restart needed."""
    dev = get_or_404(db, device_id)
    dev.rules = rules.model_dump()
    dev.rules_version += 1
    db.commit()
    hub.publish("rules", {"device_id": dev.id, "version": dev.rules_version})
    return dev.rules


@router.post("/{device_id}/worker/{action}")
async def worker_action(device_id: int, action: str, request: Request, db: Session = Depends(get_db)):
    dev = get_or_404(db, device_id)
    if action == "stop":
        manager.stop(dev.stream, manual=True)   # reconcile will not restart it
    elif action in ("start", "restart"):
        if (dev.app, dev.stream) not in await online_set(request):
            raise HTTPException(409, "stream is offline")
        manager.restart(dev)
    else:
        raise HTTPException(400, "action must be start|stop|restart")
    return manager.status(dev.stream)
