from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class FallRule(BaseModel):
    """摔倒检测 (worker/fall.py). Used only by devices with task "pose"."""
    enabled: bool = True
    confirm_sec: float = Field(2, ge=0.5, le=30)    # must stay down this long
    window_sec: float = Field(3, ge=0.5, le=10)     # standing -> lying within this time = a fall


class Rules(BaseModel):
    confirm_frames: int = Field(3, ge=1, le=50)
    cooldown_sec: float = Field(60, ge=0)
    zones: list[Zone] = []
    lines: list[Line] = []
    fall: FallRule = FallRule()


class VideoFeatures(BaseModel):
    """Per-device video features (worker/video.py). All off = what the worker always did."""
    dyn_gop: bool = False       # 动态关键帧: 10 s interval + keyframe on viewer / activity / alarm
    roi: bool = False           # ROI 编码: target boxes at a lower QP, background pays under the cap
    skip_static: bool = False   # 画面静止时跳过推理: no YOLO while the picture does not change
    yuv_direct: bool = False    # YUV 直通: draw + encode on yuv420p, convert only frames that are analysed
    rect_input: bool = False    # 矩形输入: pad only to a multiple of 32 (640x384 for 16:9), not to 640x640
    int8: bool = False          # INT8 模型: the quantized model (scripts/quantize_int8.py) instead of FP32
    compare: bool = False       # 对比模式: also push a baseline encode (all features off) to ai_ref/


class DeviceIn(BaseModel):
    name: str
    app: str = "live"
    stream: str = Field(pattern=r"^[A-Za-z0-9_\-]+$")
    source_url: str | None = None
    ai_enabled: bool = True
    det_fps: float = Field(5, gt=0, le=30)
    classes: str = "person,bicycle,car,motorcycle,bus,truck"
    task: Literal["detect", "pose"] = "detect"


class DeviceUpdate(BaseModel):
    name: str | None = None
    source_url: str | None = None
    ai_enabled: bool | None = None
    det_fps: float | None = Field(None, gt=0, le=30)
    classes: str | None = None
    task: Literal["detect", "pose"] | None = None


class DeviceOut(DeviceIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    rules: Rules
    rules_version: int
    video: VideoFeatures = VideoFeatures()

    @field_validator("video", mode="before")
    @classmethod
    def _video_default(cls, v):
        return v or {}      # devices created before the column existed: NULL

    @field_validator("task", mode="before")
    @classmethod
    def _task_default(cls, v):
        return v or "detect"
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
