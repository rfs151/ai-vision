"""Sightings for AI 查询: one record per tracked person / vehicle, with its clearest crop.

Alarms only cover what a rule caught; a search like "超市里穿绿色外套的顾客" has to find everyone
who passed. For every confirmed track this keeps the best crop seen so far (big, confident, not
cut off by the frame edge) and, when the track ends (or has lasted MAX_AGE_S), hands it to the
sink: crop JPEG + a small frame picture with the box drawn + times + tags. The API turns the
crop into a CLIP vector (api/search.py), so the worker only pays for a JPEG now and then.

riding: a person whose box sits on a bicycle / motorcycle box in at least RIDE_MIN of the frames.
"""
import time

import cv2
import numpy as np

CLASSES = {"person", "car", "truck", "bus", "motorcycle"}
BIKES = {"bicycle", "motorcycle"}
MIN_H = {"person": 60}          # px in the source frame; smaller crops tell CLIP nothing
MIN_H_DEFAULT = 40
MIN_HITS = 5                    # analysed frames before a track counts (drops flicker)
MIN_SEC = 1.5                   # tracks shorter than this are mostly fragments of an ID switch
MAX_AGE_S = 120.0               # someone standing for minutes: report once, then again later
RIDE_MIN = 0.4
FRAME_W = 640                   # width of the context picture


def _quality(box, score, w, h):
    x1, y1, x2, y2 = box
    q = (x2 - x1) * (y2 - y1) * score
    if x1 <= 2 or y1 <= 2 or x2 >= w - 2 or y2 >= h - 2:
        q *= 0.4                # cut off by the edge: only if nothing better comes
    return q


def _riding(p, bikes):
    px1, py1, px2, py2 = p
    for b in bikes:
        bx1, by1, bx2, by2 = b
        ix = max(0.0, min(px2, bx2) - max(px1, bx1))
        iy = max(0.0, min(py2, by2) - max(py1, by1))
        if ix * iy > 0.3 * (bx2 - bx1) * (by2 - by1) and (py1 + py2) / 2 < (by1 + by2) / 2:
            return True
    return False


class Sightings:
    def __init__(self, emit):
        self.emit = emit            # callback(meta: dict, crop_jpg: bytes, frame_jpg: bytes)
        self.best = {}              # track id -> state

    def reset(self):
        self.best.clear()

    def update(self, tracks, alive, removed, frame, get_bgr, t):
        """tracks: tracker output (tracked now); alive: ids of all tracks the tracker still keeps
        (incl. briefly lost ones); removed: ids it dropped this update; get_bgr(): the full BGR
        frame (called only when a crop improves, at most once per update)."""
        h, w = frame.shape[:2]
        bikes = [tr.box for tr in tracks if tr.name in BIKES and tr.t_last_seen == t]
        img = None
        now = time.time()
        for tr in tracks:
            if tr.name not in CLASSES or tr.t_last_seen != t:     # only boxes detected this frame
                continue
            s = self.best.get(tr.id)
            if s is None:
                s = self.best[tr.id] = {"cls": tr.name, "first": now, "t0": t, "q": 0.0, "hits": 0, "ride": 0,
                                        "crop": None, "frame": None, "box": None, "score": 0.0}
            s["hits"] += 1
            s["last"], s["t1"] = now, t
            if tr.name == "person" and _riding(tr.box, bikes):
                s["ride"] += 1
            x1, y1, x2, y2 = tr.box
            if y2 - y1 < MIN_H.get(tr.name, MIN_H_DEFAULT):
                continue
            q = _quality(tr.box, tr.score, w, h)
            if q <= s["q"] * 1.15:
                continue
            if img is None:
                img = get_bgr()
            pad = 0.06
            bx = [max(0, int(x1 - (x2 - x1) * pad)), max(0, int(y1 - (y2 - y1) * pad)),
                  min(w, int(x2 + (x2 - x1) * pad)), min(h, int(y2 + (y2 - y1) * pad))]
            crop = img[bx[1]:bx[3], bx[0]:bx[2]]
            if crop.size == 0:
                continue
            r = FRAME_W / w             # context picture, copied now (the frame buffer gets reused)
            small = cv2.resize(img, (FRAME_W, round(h * r)), interpolation=cv2.INTER_AREA)
            sx1, sy1, sx2, sy2 = (np.array(tr.box) * r).astype(int)
            cv2.rectangle(small, (int(sx1), int(sy1)), (int(sx2), int(sy2)), (60, 220, 60), 2)
            s.update(q=q, crop=crop.copy(), box=[float(v) for v in tr.box], score=float(tr.score), small=small)
        for tid in list(self.best):
            s = self.best[tid]
            if tid in removed or (s["crop"] is not None and now - s["first"] > MAX_AGE_S):
                self._send(s, w, h)
                del self.best[tid]
            elif tid not in alive:
                del self.best[tid]  # gone without a removal notice (tracker reset)

    def _send(self, s, w, h):
        if s["crop"] is None or s["hits"] < MIN_HITS or s["t1"] - s["t0"] < MIN_SEC:
            return
        ok1, crop = cv2.imencode(".jpg", s["crop"], [cv2.IMWRITE_JPEG_QUALITY, 90])
        ok2, frame = cv2.imencode(".jpg", s["small"], [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not (ok1 and ok2):
            return
        tags = ["riding"] if s["cls"] == "person" and s["ride"] >= RIDE_MIN * s["hits"] else []
        meta = {"cls": s["cls"], "first_seen": s["first"], "last_seen": s["last"], "score": s["score"],
                "box": s["box"], "frame_size": [w, h], "tags": tags}
        self.emit(meta, crop.tobytes(), frame.tobytes())
