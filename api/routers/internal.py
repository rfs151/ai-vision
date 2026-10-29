"""Endpoints called by AI workers (localhost only, or with X-Internal-Token)."""
import json
import os
import re
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import settings
from api.db import get_db
from api.hub import hub
from api.manager import manager
from api.notify import notifier
from api.models import Alarm, Device
from api.routers.alarms import alarm_out

TOKEN = os.getenv("INTERNAL_TOKEN", "")
LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}


def internal_only(request: Request):
    if TOKEN:
        if request.headers.get("X-Internal-Token") != TOKEN:
            raise HTTPException(403, "bad internal token")
    elif request.client is None or request.client.host not in LOCAL:
        raise HTTPException(403, "internal api is localhost only")


router = APIRouter(prefix="/api/internal", tags=["internal"], dependencies=[Depends(internal_only)])


def device_by_stream(db, stream):
    dev = db.scalar(select(Device).where(Device.stream == stream))
    if not dev:
        raise HTTPException(404, "unknown stream")
    return dev


@router.get("/rules/{stream}")
def rules(stream: str, db: Session = Depends(get_db)):
    dev = device_by_stream(db, stream)
    return {"version": dev.rules_version, "rules": dev.rules}


@router.post("/heartbeat/{stream}")
def heartbeat(stream: str, stats: dict):
    if not manager.heartbeat(stream, stats):
        # Worker started by hand (not by the manager): accept, nothing to supervise
        return {"managed": False}
    hub.publish("worker", manager.status(stream))
    return {"managed": True}


@router.post("/alarms", status_code=201)
async def ingest_alarm(event: str = Form(...), snapshot: UploadFile | None = File(None),
                       db: Session = Depends(get_db)):
    e = json.loads(event)
    dev = device_by_stream(db, e["stream"])
    rel = None
    if snapshot is not None:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{e['type']}_{e['rule_id']}_{e.get('track_id', 0)}")
        day = time.strftime("%Y%m%d")
        rel = f"alarms/{dev.stream}/{day}/{time.strftime('%H%M%S')}_{int(time.time() * 1000) % 1000:03d}_{safe}.jpg"
        path = settings.data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(await snapshot.read())
    a = Alarm(device_id=dev.id, type=e["type"], rule_id=e["rule_id"], rule_name=e.get("rule_name", ""),
              track_id=e.get("track_id", 0), cls=e.get("cls", ""), score=e.get("score", 0),
              box=list(e.get("box") or []), detail=e.get("detail") or {}, snapshot=rel,
              stream_time=e.get("t", 0))
    db.add(a)
    db.commit()
    out = alarm_out(a, dev.stream)
    hub.publish("alarm", out.model_dump(mode="json"))
    notifier.alarm(out, dev.name, settings.data_dir / rel if rel else None)
    return {"id": a.id}


@router.post("/clips", status_code=201)
async def ingest_clip(alarm_ids: str = Form(...), start: float = Form(0), clip: UploadFile = File(...),
                      db: Session = Depends(get_db)):
    """One MP4 shared by the alarms close together in time (worker merges them)."""
    ids = [int(i) for i in json.loads(alarm_ids)]
    alarms = list(db.scalars(select(Alarm).where(Alarm.id.in_(ids))))
    if not alarms:
        raise HTTPException(404, "alarms not found")
    dev = db.get(Device, alarms[0].device_id)
    t = time.localtime(start or time.time())
    rel = f"clips/{dev.stream}/{time.strftime('%Y%m%d', t)}/{time.strftime('%H%M%S', t)}_{min(ids)}.mp4"
    path = settings.data_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(await clip.read())
    for a in alarms:
        a.clip = rel
    db.commit()
    hub.publish("alarm_clip", {"ids": [a.id for a in alarms], "clip_url": f"/media/{rel}"})
    return {"clip_url": f"/media/{rel}", "alarms": len(alarms)}
