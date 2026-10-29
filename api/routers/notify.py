"""Push settings (WeCom / DingTalk robots), stored in the settings table under "notify"."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.db import SessionLocal, get_db
from api.models import Setting
from api.notify import MASK, NotifyConfig, notifier

router = APIRouter(prefix="/api/notify", tags=["notify"])
KEY = "notify"


def load():
    """Called at startup."""
    with SessionLocal() as db:
        row = db.get(Setting, KEY)
        if row:
            notifier.cfg = NotifyConfig(**row.value)


@router.get("")
def get_config():
    return {**notifier.cfg.masked(), "status": notifier.status()}


@router.put("")
def put_config(cfg: NotifyConfig, db: Session = Depends(get_db)):
    if cfg.dingtalk_secret == MASK:          # unchanged secret comes back masked
        cfg.dingtalk_secret = notifier.cfg.dingtalk_secret
    row = db.get(Setting, KEY) or Setting(key=KEY)
    row.value = cfg.model_dump()
    db.add(row)
    db.commit()
    notifier.cfg = cfg
    return {**cfg.masked(), "status": notifier.status()}


@router.post("/test")
async def test():
    """Sends a test message with the saved settings (works even when pushing is disabled)."""
    return {"result": await notifier.test(), "status": notifier.status()}
