from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.db import Base

DEFAULT_RULES = {"confirm_frames": 3, "cooldown_sec": 60, "zones": [], "lines": []}


class Device(Base):
    """A video source. Either pushed to ZLM (live/<stream>) or pulled from a camera (source_url)."""
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    app: Mapped[str] = mapped_column(String(50), default="live")
    stream: Mapped[str] = mapped_column(String(100), unique=True)  # also the ai/<stream> output name
    source_url: Mapped[str | None] = mapped_column(String(500))   # rtsp://camera... -> ZLM addStreamProxy
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    det_fps: Mapped[float] = mapped_column(Float, default=5)
    classes: Mapped[str] = mapped_column(String(200), default="person,bicycle,car,motorcycle,bus,truck")
    rules: Mapped[dict] = mapped_column(JSON, default=lambda: dict(DEFAULT_RULES))
    rules_version: Mapped[int] = mapped_column(Integer, default=1)
    # Local time (not SQLite CURRENT_TIMESTAMP, which is UTC): the UI and filters use local time
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    alarms = relationship("Alarm", back_populates="device", cascade="all, delete-orphan")


class Alarm(Base):
    __tablename__ = "alarms"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(30), index=True)    # intrusion|dwell|crowd|line_in|line_out
    rule_id: Mapped[str] = mapped_column(String(50))
    rule_name: Mapped[str] = mapped_column(String(100))
    track_id: Mapped[int] = mapped_column(Integer, default=0)
    cls: Mapped[str] = mapped_column(String(50), default="")
    score: Mapped[float] = mapped_column(Float, default=0)
    box: Mapped[list] = mapped_column(JSON, default=list)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    snapshot: Mapped[str | None] = mapped_column(String(300))   # path relative to data dir
    stream_time: Mapped[float] = mapped_column(Float, default=0)  # frame pts (s)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)   # new|ack|false

    device = relationship("Device", back_populates="alarms")
