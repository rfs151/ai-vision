"""AI 查询: question parsing, sighting ingest / merge, ranking, and the worker-side collector."""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DATA_DIR", _tmp)
os.environ.setdefault("DB_URL", f"sqlite:///{_tmp}/test.db")
os.environ.setdefault("ZLM_SECRET", "x")

from api import search  # noqa: E402
from api.config import settings  # noqa: E402
from api.db import SessionLocal, migrate  # noqa: E402
from api.models import Device, Sighting  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
from sightings import Sightings  # noqa: E402

CAMS = dict(search.CAMERA_HINTS)


@pytest.mark.parametrize("q, want", [
    ("超市里穿绿色外套的顾客", {"devices": ["store"], "classes": ["person"], "appearance": "穿绿色外套的人"}),
    ("最近 10 分钟园区路口穿红色上衣的行人",
     {"since_minutes": 10, "devices": ["ped"], "classes": ["person"], "appearance": "穿红色上衣的人"}),
    ("最近三天穿黑衣服骑车的人", {"since_minutes": 4320, "classes": ["person"], "tags": ["riding"]}),
    ("快速路上的白色货车", {"devices": ["cars"], "classes": ["truck"], "appearance": "白色货车"}),
])
def test_rule_parser(q, want):
    got = search.clean(search.parse_rules(q, CAMS), CAMS)
    for k, v in want.items():
        assert got[k] == v, (k, got)


def test_clean_drops_unknown_values():
    got = search.clean({"since_minutes": -5, "devices": ["store", "mars"], "classes": ["dog"],
                        "tags": ["flying"], "appearance": "  穿红衣服的人 "}, CAMS)
    assert got == {"devices": ["store"], "appearance": "穿红衣服的人"}


def test_llm_tool_call(monkeypatch):
    """DeepSeek path: the tool call's arguments become the conditions (still checked by clean())."""
    sent = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            args = {"devices": ["store"], "classes": ["person"], "appearance": "穿绿色外套的人", "since_minutes": 30}
            return {"choices": [{"message": {"tool_calls": [{"function": {"arguments": json.dumps(args)}}]}}]}

    def post(self, url, json=None, headers=None):
        sent.update(url=url, body=json)
        return Resp()

    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr("httpx.Client.post", post)
    got = search.clean(search.parse_llm("超市里穿绿色外套的顾客", CAMS), CAMS)
    assert got == {"since_minutes": 30, "devices": ["store"], "classes": ["person"], "appearance": "穿绿色外套的人"}
    assert sent["url"].endswith("/chat/completions")
    assert sent["body"]["tool_choice"]["function"]["name"] == "search_sightings"
    assert "store = 超市" in json.dumps(sent["body"]["tools"], ensure_ascii=False)


class FakeClip:
    """Vectors from the crop's mean colour, texts from a fixed table: green / brown are far apart."""
    available = True
    TEXT = {"穿绿色外套的人": [0, 1, 0], "穿棕色外套的人": [1, 0.4, 0]}

    def image(self, bgr):
        b, g, r = bgr.reshape(-1, 3).mean(0)
        v = np.zeros(512, np.float32)
        v[:3] = (r, g, b)
        return v / np.linalg.norm(v)

    def text(self, s):
        v = np.zeros(512, np.float32)
        v[:3] = self.TEXT.get(s, [1, 1, 1])
        return v / np.linalg.norm(v)


def _crop(bgr_color, name):
    rel = f"sightings/test/{name}.jpg"
    p = settings.data_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(p), np.full((120, 60, 3), bgr_color, np.uint8))
    return rel


def test_ingest_merges_repeats_and_ranks_by_appearance(monkeypatch):
    migrate()
    monkeypatch.setattr(search, "clip", FakeClip())
    monkeypatch.setattr(settings, "llm_api_key", "")
    with SessionLocal() as db:
        dev = db.query(Device).filter_by(stream="store").first()
        if dev is None:
            dev = Device(name="store", stream="store")
            db.add(dev)
            db.commit()
        dev_id = dev.id
        db.query(Sighting).delete()
        db.commit()
    meta = {"cls": "person", "tags": [], "score": 0.9, "box": [0, 0, 60, 120]}
    search._ingest(dev_id, meta, _crop((40, 160, 40), "green1"), None)       # green coat
    search._ingest(dev_id, meta, _crop((42, 158, 41), "green2"), None)       # same person, next loop
    search._ingest(dev_id, meta, _crop((30, 70, 140), "brown"), None)        # brown jacket
    with SessionLocal() as db:
        rows = db.query(Sighting).order_by(Sighting.id).all()
        assert [r.count for r in rows] == [2, 1]
        assert not (settings.data_dir / "sightings/test/green2.jpg").exists()   # merged: files removed
        res = search.search(db, "超市里穿绿色外套的顾客")
    assert res["source"] == "rules" and res["conditions"]["devices"] == ["store"]
    assert res["results"][0]["count"] == 2                                   # the green one first
    assert len(res["results"]) == 1                                          # brown is far below the best


def test_collector_sends_best_crop_when_track_ends():
    sent = []
    col = Sightings(lambda meta, crop, frame: sent.append((meta, crop, frame)))
    frame = np.zeros((480, 640, 3), np.uint8)

    def tr(i, name, box, t, score=0.8):
        return SimpleNamespace(id=i, name=name, box=np.array(box, float), score=score, t_last_seen=t)

    for k in range(6):                  # a person riding a bicycle for 2.5 s, growing as it comes closer
        t = k * 0.5
        tracks = [tr(1, "person", [100, 100 - k * 5, 160, 260 + k * 10], t), tr(2, "bicycle", [95, 200, 170, 300], t)]
        col.update(tracks, {1, 2}, set(), frame, lambda: frame, t)
    assert not sent                     # still on screen
    col.update([], {2}, {1}, frame, lambda: frame, 5.0)
    assert len(sent) == 1
    meta, crop, small = sent[0]
    assert meta["cls"] == "person" and meta["tags"] == ["riding"]
    assert meta["box"][3] >= 300.0      # a late, large box was kept (replaced only when 15% better)
    col.update([tr(3, "person", [300, 100, 360, 260], 6.0)], {3}, set(), frame, lambda: frame, 6.0)
    col.update([], set(), {3}, frame, lambda: frame, 6.2)
    assert len(sent) == 1               # one frame only: a fragment, not sent
    assert cv2.imdecode(np.frombuffer(small, np.uint8), cv2.IMREAD_COLOR).shape[1] == 640
