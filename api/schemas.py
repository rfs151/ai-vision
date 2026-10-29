from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Zone(BaseModel):
    id: str
    name: str = ""
    polygon: list[tuple[float, float]] = Field(min_length=3)
    classes: list[str] = []
    intrusion: bool = True
    dwell_sec: float | None = None
    max_count: int | None = None


class Line(BaseModel):
    id: str
    name: str = ""
    p1: tuple[float, float]
    p2: tuple[float, float]
    classes: list[str] = []
    alarm_on: Literal["in", "out", "both"] | None = None


class Rules(BaseModel):
    confirm_frames: int = Field(3, ge=1, le=50)
    cooldown_sec: float = Field(60, ge=0)
    zones: list[Zone] = []
    lines: list[Line] = []


class DeviceIn(BaseModel):
    name: str
    app: str = "live"
    stream: str = Field(pattern=r"^[A-Za-z0-9_\-]+$")
    source_url: str | None = None
    ai_enabled: bool = True
    det_fps: float = Field(5, gt=0, le=30)
    classes: str = "person,bicycle,car,motorcycle,bus,truck"


class DeviceUpdate(BaseModel):
    name: str | None = None
    source_url: str | None = None
    ai_enabled: bool | None = None
    det_fps: float | None = Field(None, gt=0, le=30)
    classes: str | None = None


class DeviceOut(DeviceIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    rules: Rules
    rules_version: int
    created_at: datetime
    online: bool = False
    worker: dict | None = None
    play: dict = {}


class AlarmOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: int
    stream: str = ""
    type: str
    rule_id: str
    rule_name: str
    track_id: int
    cls: str
    score: float
    box: list
    detail: dict
    snapshot_url: str | None = None
    clip_url: str | None = None
    stream_time: float
    created_at: datetime
    status: str


class AlarmPage(BaseModel):
    total: int
    items: list[AlarmOut]


class AlarmStatus(BaseModel):
    status: Literal["new", "ack", "false"]
