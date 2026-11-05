"""画面静止时跳过推理 (skip_static): motion from the source stream's own motion vectors.

The camera's encoder already did motion estimation; with `flags2=+export_mvs` the decoder hands
its motion vectors out with each frame, so measuring motion costs almost nothing (no pixel work).

Motion of a frame = share of the picture that moved by >= 1 px, plus the share coded without a
motion vector (intra blocks in a P frame: usually new content). Measured on the samples: a still
picture with sensor noise sits at 0.2-0.6 %, people walking at 4 % and up. I frames carry no
vectors and count as "unknown".

Between two analysis rounds the average motion of the frames decides (one noisy frame does not
count; a person walking keeps it above the threshold for many frames): below THRESHOLD the round
skips YOLO and hands the previous detections to the tracker and rules again (tracks stay alive,
dwell timers keep running). At least one real inference every MAX_SKIP_S, however still the
picture is, so a target moving very slowly is not missed for long.
"""
import threading
import time
from collections import deque

import av
import numpy as np

MV = av.sidedata.sidedata.Type.MOTION_VECTORS
THRESHOLD = 0.01        # 1 % of the picture
MAX_SKIP_S = 2.0
WINDOW_S = 20.0


def motion_fraction(frame):
    """Share of the picture that changed (0..1), or None for a frame without motion vectors."""
    sd = frame.side_data.get(MV)
    if sd is None:
        return None
    a = sd.to_ndarray()
    if not len(a):
        return None
    fwd = a["source"] < 0                       # one reference per block (no double counting)
    blk = a["w"][fwd].astype(np.int64) * a["h"][fwd]
    mag = np.hypot(a["motion_x"][fwd], a["motion_y"][fwd]) / np.maximum(a["motion_scale"][fwd], 1)
    area = frame.width * frame.height
    moving = blk[mag >= 1].sum() / area
    intra = max(0.0, 1.0 - blk.sum() / area)
    return float(min(1.0, moving + intra))


class MotionGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._sum, self._n = 0.0, 0     # motion of the frames since the last analysis round
        self._last_run = None           # monotonic time of the last real inference
        self._motion = deque()          # (time, motion) per frame, for stats
        self._rounds = deque()          # (time, ran inference)

    def reset(self):
        with self._lock:
            self._sum, self._n, self._last_run = 0.0, 0, None

    def add(self, m):
        """Decode thread, every frame."""
        if m is None:
            return
        now = time.monotonic()
        with self._lock:
            self._sum += m
            self._n += 1
            self._motion.append((now, m))

    def skip(self, enabled, have_previous):
        """Analysis thread, once per round: True = reuse the previous detections."""
        now = time.monotonic()
        with self._lock:
            m = self._sum / self._n if self._n else None
            self._sum, self._n = 0.0, 0
            still = m is not None and m < THRESHOLD
            fresh = self._last_run is not None and now - self._last_run < MAX_SKIP_S
            skip = bool(enabled and have_previous and still and fresh)
            if not skip:
                self._last_run = now
            self._rounds.append((now, not skip))
        return skip

    def stats(self):
        now = time.monotonic()
        with self._lock:
            for q in (self._motion, self._rounds):
                while q and now - q[0][0] > WINDOW_S:
                    q.popleft()
            ms = [m for _, m in self._motion]
            rounds = list(self._rounds)
        span = min(WINDOW_S, now - rounds[0][0]) if rounds else 0
        ran = sum(1 for _, r in rounds if r)
        return {
            "motion_pct": round(sum(ms) / len(ms) * 100, 2) if ms else None,
            "infer_per_s": round(ran / span, 2) if span >= 1 else None,
            "skip_pct": round((1 - ran / len(rounds)) * 100) if rounds else None,
        }
