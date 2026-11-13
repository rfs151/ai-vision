"""AI 查询: find people / vehicles from one sentence, e.g. "超市里穿绿色外套的顾客".

    question ─> parse (DeepSeek tool call; rule parser without a key or when the call fails)
             ─> {"devices": ["store"], "classes": ["person"], "appearance": "穿绿色外套的人", ...}
             ─> SQL on sightings (time, camera, class, tags) ─> CLIP text vector vs crop vectors ─> ranked

Only the question and the camera list go to the LLM; crops, frames and vectors stay here.
The model never writes SQL: its JSON is checked field by field (unknown cameras / classes dropped).

Ingest (worker/sightings.py -> POST /api/internal/sightings): files are saved at once, the CLIP
image vector is computed on one background thread (about 0.1-0.3 s per crop on a busy CPU).
A looping demo video shows the same people every minute: a new sighting whose vector is close
to one of the same camera and class from the last hours (cosine >= MERGE_COS) is merged into it
(count + 1, last_seen updated) instead of becoming a new row.
"""
import json
import logging
import queue
import re
import threading
import time
from datetime import datetime, timedelta

import httpx
import numpy as np
from sqlalchemy import delete, select

from api.clip import clip
from api.config import settings
from api.db import SessionLocal
from api.models import Device, Sighting

log = logging.getLogger("search")
MERGE_COS = 0.93
MERGE_HOURS = 6
# Cheap pre-merge, before CLIP: a looping video replays the same frames, so the same person comes
# back at (almost) the same box with (almost) the same pixels. Box IoU + a 8x16 colour thumbnail
# catch most repeats for ~0.1 ms instead of a CLIP pass (~0.3-1 s on a busy CPU).
QUICK_IOU = 0.6
QUICK_COS = 0.985
CANDIDATES = 3000               # newest rows scored per search
TOP = 24
KEEP_BELOW_BEST = 0.06          # appearance search: drop results this far below the best score

CLASSES = {"person": "人", "car": "小汽车", "truck": "卡车 / 货车", "bus": "公交车 / 大巴",
           "motorcycle": "摩托车 / 电动车"}
TAGS = {"riding": "骑自行车、电动车或摩托车的人"}
# Chinese names for the demo cameras, so "超市" finds store. Override / extend: SEARCH_CAMERAS env
# (JSON {"stream": "描述"}); cameras not listed are offered by name only.
CAMERA_HINTS = {
    "store": "超市 / 商店货架通道（顾客、购物）",
    "still": "超市 / 商店货架（另一机位，人少，画面常静止）",
    "ped": "园区路口 / 室外道路（行人、车辆）",
    "people": "室内走廊 / 办公室门口",
    "street": "停车场（俯拍，行人）",
    "worker": "工厂作业区（工人、反光背心、安全帽）",
    "cars": "城市快速路（车流）",
    "hd": "城市快速路（车流，1080p）",
    "fall": "办公室（姿态识别 / 摔倒检测）",
}
EXAMPLES = ["超市里穿绿色外套的顾客", "超市里戴帽子的男人", "最近 10 分钟园区路口穿红色上衣的行人",
            "停车场里穿深色衣服的人", "快速路上的白色货车"]


# ---------------------------------------------------------------- ingest
_q = queue.Queue(maxsize=500)


def enqueue(device_id, meta, crop_rel, frame_rel):
    try:
        _q.put_nowait((device_id, meta, crop_rel, frame_rel))
    except queue.Full:
        log.warning("sighting queue full, dropped")
        for rel in filter(None, (crop_rel, frame_rel)):
            (settings.data_dir / rel).unlink(missing_ok=True)


def _vec(blob):
    return np.frombuffer(blob, dtype=np.float16).astype(np.float32)


_recent = {}                    # (device_id, cls) -> [(sighting id, unit box, thumb, time)], this process only


def _thumb(img):
    import cv2
    t = cv2.resize(img, (8, 16), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()
    t -= t.mean()
    return t / max(float(np.linalg.norm(t)), 1e-6)


def _unit_box(meta):
    w, h = (meta.get("frame_size") or [1, 1])[:2]
    b = meta.get("box") or [0, 0, 0, 0]
    return np.array([b[0] / w, b[1] / h, b[2] / w, b[3] / h], dtype=np.float32)


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _merge(db, sid, last, meta, crop_rel, frame_rel):
    s = db.get(Sighting, sid)
    if s is None:
        return False
    s.count += 1
    s.last_seen = max(s.last_seen, last)
    s.tags = sorted(set(s.tags or []) | set(meta.get("tags") or []))
    db.commit()
    for rel in filter(None, (crop_rel, frame_rel)):
        (settings.data_dir / rel).unlink(missing_ok=True)
    return True


def _ingest(device_id, meta, crop_rel, frame_rel):
    import cv2
    first = datetime.fromtimestamp(meta.get("first_seen") or time.time())
    last = datetime.fromtimestamp(meta.get("last_seen") or time.time())
    img = cv2.imread(str(settings.data_dir / crop_rel))
    key = (device_id, meta["cls"])
    box, thumb = _unit_box(meta), (_thumb(img) if img is not None else None)
    now = time.time()
    recent = [r for r in _recent.get(key, []) if now - r[3] < MERGE_HOURS * 3600]
    _recent[key] = recent
    if thumb is not None:
        for sid, b, t, _ in recent:
            if _iou(box, b) >= QUICK_IOU and float(t @ thumb) >= QUICK_COS:
                with SessionLocal() as db:
                    if _merge(db, sid, last, meta, crop_rel, frame_rel):
                        return
    emb = clip.image(img) if clip.available and img is not None else None
    with SessionLocal() as db:
        if emb is not None:
            since = datetime.now() - timedelta(hours=MERGE_HOURS)
            rows = db.execute(select(Sighting.id, Sighting.embedding).where(
                Sighting.device_id == device_id, Sighting.cls == meta["cls"],
                Sighting.last_seen >= since, Sighting.embedding.is_not(None))).all()
            if rows:
                sims = np.stack([_vec(r.embedding) for r in rows]) @ emb
                j = int(np.argmax(sims))
                if sims[j] >= MERGE_COS and _merge(db, rows[j].id, last, meta, crop_rel, frame_rel):
                    if thumb is not None:
                        recent.append((rows[j].id, box, thumb, now))     # next loop: quick match
                    return
        s = Sighting(device_id=device_id, cls=meta["cls"], tags=meta.get("tags") or [],
                        score=meta.get("score", 0), box=meta.get("box") or [], crop=crop_rel, frame=frame_rel,
                        embedding=emb.astype(np.float16).tobytes() if emb is not None else None,
                        first_seen=first, last_seen=last)
        db.add(s)
        db.commit()
        if thumb is not None:
            recent.append((s.id, box, thumb, now))
        del recent[:-500]


def _ingest_loop():
    while True:
        item = _q.get()
        try:
            _ingest(*item)
        except Exception:
            log.exception("sighting ingest failed")


def start():
    threading.Thread(target=_ingest_loop, daemon=True, name="sightings").start()


def prune():
    """Delete sightings (rows + files) older than SIGHTING_KEEP_DAYS. Called by retention.loop()."""
    cut = datetime.now() - timedelta(days=settings.sighting_keep_days)
    with SessionLocal() as db:
        old = db.execute(select(Sighting.crop, Sighting.frame).where(Sighting.last_seen < cut)).all()
        for r in old:
            for rel in (r.crop, r.frame):
                if rel:
                    (settings.data_dir / rel).unlink(missing_ok=True)
        db.execute(delete(Sighting).where(Sighting.last_seen < cut))
        db.commit()
    return len(old)


# ---------------------------------------------------------------- parse
def cameras(db):
    hints = dict(CAMERA_HINTS)
    try:
        import os
        hints.update(json.loads(os.getenv("SEARCH_CAMERAS", "{}")))
    except ValueError:
        pass
    return {d.stream: hints.get(d.stream, d.name) for d in db.scalars(select(Device))}


def tool_schema(cams):
    return {"type": "function", "function": {
        "name": "search_sightings",
        "description": "按条件查找摄像头拍到的人或车辆。只填用户提到的条件，没提到的不要填。",
        "parameters": {"type": "object", "properties": {
            "since_minutes": {"type": "integer", "description": "最近多少分钟，例如最近三天 = 4320，最近 1 小时 = 60；没说时间就不填"},
            "devices": {"type": "array", "items": {"type": "string", "enum": list(cams)},
                        "description": "摄像头。" + "；".join(f"{k} = {v}" for k, v in cams.items())},
            "classes": {"type": "array", "items": {"type": "string", "enum": list(CLASSES)},
                        "description": "目标类别：" + "，".join(f"{k} = {v}" for k, v in CLASSES.items())
                                       + "。顾客、行人、工人都是 person"},
            "tags": {"type": "array", "items": {"type": "string", "enum": list(TAGS)},
                     "description": "；".join(f"{k} = {v}" for k, v in TAGS.items())},
            "appearance": {"type": "string",
                           "description": "外观描述，改写成“穿……的人”“白色的货车”这样的短句，只保留衣着、颜色、"
                                          "配饰、车型等看得见的特征；去掉时间、地点。没有外观条件就不填"},
        }}}}


def parse_llm(question, cams):
    if not settings.llm_api_key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")
    body = {"model": settings.llm_model, "temperature": 0, "max_tokens": 300,
            "messages": [{"role": "system", "content": "你把监控查询语句转换成 search_sightings 的参数。"
                                                       f"现在是 {datetime.now():%Y-%m-%d %H:%M}。"},
                         {"role": "user", "content": question}],
            "tools": [tool_schema(cams)],
            "tool_choice": {"type": "function", "function": {"name": "search_sightings"}}}
    with httpx.Client(timeout=15, trust_env=False) as h:
        r = h.post(settings.llm_base_url.rstrip("/") + "/chat/completions", json=body,
                   headers={"Authorization": f"Bearer {settings.llm_api_key}"})
        r.raise_for_status()
    calls = r.json()["choices"][0]["message"].get("tool_calls") or []
    return json.loads(calls[0]["function"]["arguments"]) if calls else {}


_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
           "半": 0.5}
_UNIT = {"分钟": 1, "分": 1, "小时": 60, "个小时": 60, "天": 1440, "日": 1440, "周": 10080, "星期": 10080}
_CLASS_WORDS = [("motorcycle", "摩托|电动车|电瓶车"), ("truck", "货车|卡车"), ("bus", "公交|大巴|客车"),
                ("car", "小汽车|轿车|汽车|车辆"), ("person", "人|顾客|行人|工人|男|女")]
_CAM_WORDS = {"store": "超市|商店|货架", "ped": "路口|园区", "people": "走廊|室内", "street": "停车场",
              "worker": "作业区|工厂|车间", "cars": "快速路|公路|高速", "fall": "办公室"}


def parse_rules(question, cams):
    """Fallback without an LLM: common phrasings only."""
    q = question.strip()
    out = {}
    m = re.search(r"(?:最近|过去|近)\s*([0-9]+(?:\.[0-9]+)?|[一两二三四五六七八九十半])\s*(个小时|小时|分钟|分|天|日|周|星期)", q)
    if m:
        n = float(m.group(1)) if m.group(1)[0].isdigit() else _CN_NUM[m.group(1)]
        out["since_minutes"] = int(n * _UNIT[m.group(2)])
        q = q.replace(m.group(0), " ")
    elif "今天" in q:
        out["since_minutes"] = int((datetime.now() - datetime.now().replace(hour=0, minute=0)).seconds / 60) + 1
        q = q.replace("今天", " ")
    devs = [s for s, w in _CAM_WORDS.items() if s in cams and re.search(w, q)]
    if devs:
        out["devices"] = devs
        q = re.sub("|".join(_CAM_WORDS[s] for s in devs), " ", q)
    if re.search("骑车|骑行|骑自行车|骑电动车|骑摩托", q):
        out["tags"] = ["riding"]
        out["classes"] = ["person"]
    else:
        for cls, w in _CLASS_WORDS:
            if re.search(w, q):
                out["classes"] = [cls]
                break
    look = re.sub(r"^(查找|查一下|查询|找出|找|搜索|看看)|[，。？?！!]|里|中|出现的|的人$", " ", q).strip()
    look = re.sub(r"\s+", "", look)
    look = re.sub(r"^(上的|上|的)|的$", "", look)
    if len(look) >= 2:
        out["appearance"] = look
    return out


def clean(p, cams):
    out = {}
    try:
        m = int(p.get("since_minutes") or 0)
        if 0 < m <= 60 * 24 * 365:
            out["since_minutes"] = m
    except (TypeError, ValueError):
        pass
    for k, allowed in (("devices", cams), ("classes", CLASSES), ("tags", TAGS)):
        v = [x for x in (p.get(k) or []) if isinstance(x, str) and x in allowed]
        if v:
            out[k] = v
    a = p.get("appearance")
    if isinstance(a, str) and a.strip():
        # CLIP matches "穿绿色外套的人" much better than "……的顾客": role words say nothing about looks
        # (tested on the store crops: the green-coat man ranks 1-2 instead of 3-4)
        out["appearance"] = re.sub(r"(顾客|行人|路人|工人|员工|人员|客人)$", "人", a.strip())[:60]
    return out


# ---------------------------------------------------------------- search
def search(db, question):
    t0 = time.perf_counter()
    cams = cameras(db)
    source, error = "deepseek", None
    try:
        raw = parse_llm(question, cams)
    except Exception as e:
        source, error = "rules", str(e)[:120]
        if settings.llm_api_key:
            log.warning("LLM parse failed, using rules: %s", e)
        raw = parse_rules(question, cams)
    cond = clean(raw, cams)
    parse_ms = (time.perf_counter() - t0) * 1000

    q = select(Sighting, Device.stream, Device.name).join(Device, Device.id == Sighting.device_id)
    if cond.get("since_minutes"):
        q = q.where(Sighting.last_seen >= datetime.now() - timedelta(minutes=cond["since_minutes"]))
    if cond.get("devices"):
        q = q.where(Device.stream.in_(cond["devices"]))
    if cond.get("classes"):
        q = q.where(Sighting.cls.in_(cond["classes"]))
    rows = db.execute(q.order_by(Sighting.last_seen.desc()).limit(CANDIDATES)).all()
    if cond.get("tags"):
        rows = [r for r in rows if set(cond["tags"]) <= set(r.Sighting.tags or [])]
    n_candidates = len(rows)

    scored = [(None, r) for r in rows]
    note = None
    if cond.get("appearance"):
        if clip.available:
            tv = clip.text(cond["appearance"])
            with_vec = [r for r in rows if r.Sighting.embedding]
            if with_vec:
                sims = np.stack([_vec(r.Sighting.embedding) for r in with_vec]) @ tv
                order = np.argsort(-sims)
                best = float(sims[order[0]])
                scored = [(float(sims[i]), with_vec[i]) for i in order if sims[i] >= best - KEEP_BELOW_BEST]
            else:
                scored = []
        else:
            note = "没有 CLIP 模型（scripts/export_clip.py），外观条件未使用"
    results = [{
        "id": r.Sighting.id, "stream": r.stream, "device": r.name, "camera": cams.get(r.stream, r.name),
        "cls": r.Sighting.cls, "tags": r.Sighting.tags or [], "count": r.Sighting.count,
        "first_seen": r.Sighting.first_seen.isoformat(timespec="seconds"),
        "last_seen": r.Sighting.last_seen.isoformat(timespec="seconds"),
        "crop": f"/media/{r.Sighting.crop}", "frame": f"/media/{r.Sighting.frame}" if r.Sighting.frame else None,
        "similarity": round(s, 3) if s is not None else None,
    } for s, r in scored[:TOP]]
    return {"question": question, "conditions": cond, "source": source, "llm_error": error,
            "parse_ms": round(parse_ms), "total_ms": round((time.perf_counter() - t0) * 1000),
            "candidates": n_candidates, "results": results, "note": note}


def status(db):
    from sqlalchemy import func
    n = db.scalar(select(func.count()).select_from(Sighting)) or 0
    seen = db.scalar(select(func.sum(Sighting.count))) or 0
    since = db.scalar(select(func.min(Sighting.first_seen)))
    per = dict(db.execute(select(Device.stream, func.count(Sighting.id)).select_from(Sighting)
                          .join(Device, Device.id == Sighting.device_id).group_by(Device.stream)).all())
    return {"sightings": n, "seen": int(seen), "since": since.isoformat(timespec="seconds") if since else None,
            "per_camera": per, "clip": clip.available, "llm": bool(settings.llm_api_key),
            "llm_model": settings.llm_model if settings.llm_api_key else None,
            "keep_days": settings.sighting_keep_days, "examples": EXAMPLES, "cameras": cameras(db)}
