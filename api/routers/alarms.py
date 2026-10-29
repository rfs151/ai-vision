from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.db import get_db
from api.models import Alarm, Device
from api.schemas import AlarmOut, AlarmPage, AlarmStatus

router = APIRouter(prefix="/api/alarms", tags=["alarms"])


def alarm_out(a: Alarm, stream: str) -> AlarmOut:
    out = AlarmOut.model_validate(a)
    out.stream = stream
    out.snapshot_url = f"/media/{a.snapshot}" if a.snapshot else None
    return out


@router.get("", response_model=AlarmPage)
def list_alarms(device_id: int | None = None, stream: str | None = None, type: str | None = None,
                status: str | None = None, since: datetime | None = None, until: datetime | None = None,
                page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=200),
                db: Session = Depends(get_db)):
    q = select(Alarm, Device.stream).join(Device)
    if device_id:
        q = q.where(Alarm.device_id == device_id)
    if stream:
        q = q.where(Device.stream == stream)
    if type:
        q = q.where(Alarm.type == type)
    if status:
        q = q.where(Alarm.status == status)
    if since:
        q = q.where(Alarm.created_at >= since)
    if until:
        q = q.where(Alarm.created_at < until)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.execute(q.order_by(Alarm.id.desc()).offset((page - 1) * size).limit(size)).all()
    return AlarmPage(total=total, items=[alarm_out(a, s) for a, s in rows])


@router.get("/stats")
def stats(hours: int = Query(24, ge=1, le=24 * 30), db: Session = Depends(get_db)):
    since = datetime.now() - timedelta(hours=hours)
    base = select(Alarm).where(Alarm.created_at >= since).subquery()
    by_type = dict(db.execute(select(base.c.type, func.count()).group_by(base.c.type)).all())
    by_device = {s: n for s, n in db.execute(
        select(Device.stream, func.count()).join(base, base.c.device_id == Device.id).group_by(Device.stream)).all()}
    unhandled = db.scalar(select(func.count()).select_from(base).where(base.c.status == "new"))
    return {"hours": hours, "by_type": by_type, "by_device": by_device, "unhandled": unhandled}


@router.get("/{alarm_id}", response_model=AlarmOut)
def get_alarm(alarm_id: int, db: Session = Depends(get_db)):
    row = db.execute(select(Alarm, Device.stream).join(Device).where(Alarm.id == alarm_id)).first()
    if not row:
        raise HTTPException(404, "alarm not found")
    return alarm_out(*row)


@router.patch("/{alarm_id}", response_model=AlarmOut)
def set_status(alarm_id: int, body: AlarmStatus, db: Session = Depends(get_db)):
    a = db.get(Alarm, alarm_id)
    if not a:
        raise HTTPException(404, "alarm not found")
    a.status = body.status
    db.commit()
    return alarm_out(a, a.device.stream)
