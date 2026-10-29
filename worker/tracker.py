"""ByteTrack-style multi-object tracker.

Why ByteTrack: besides high-score detections it also uses low-score ones
(often occluded / blurred targets) in a second matching round, so IDs survive
short occlusions without inventing new tracks from noise.

Time is in seconds (frame pts), not frame index, because detection runs at a
variable rate (default 5 fps) while video is drawn at full rate (e.g. 24 fps).
`TrackView.box_at(t)` extrapolates the box with the Kalman velocity, which fills
the gaps between detections so boxes do not lag behind moving targets.
"""
from dataclasses import dataclass, field
from itertools import count

import numpy as np
from scipy.optimize import linear_sum_assignment

TENTATIVE, TRACKED, LOST = 0, 1, 2


def iou_matrix(a, b):
    """IoU between boxes a [N,4] and b [M,4] in x1,y1,x2,y2."""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)), dtype=np.float32)
    a = np.asarray(a, dtype=np.float32)[:, None]
    b = np.asarray(b, dtype=np.float32)[None]
    iw = np.clip(np.minimum(a[..., 2], b[..., 2]) - np.maximum(a[..., 0], b[..., 0]), 0, None)
    ih = np.clip(np.minimum(a[..., 3], b[..., 3]) - np.maximum(a[..., 1], b[..., 1]), 0, None)
    inter = iw * ih
    area_a = (a[..., 2] - a[..., 0]) * (a[..., 3] - a[..., 1])
    area_b = (b[..., 2] - b[..., 0]) * (b[..., 3] - b[..., 1])
    return inter / np.maximum(area_a + area_b - inter, 1e-6)


def match(tracks_boxes, det_boxes, min_iou, same_class=None):
    """Hungarian matching on IoU. Returns (pairs, unmatched_tracks, unmatched_dets)."""
    iou = iou_matrix(tracks_boxes, det_boxes)
    if same_class is not None:
        iou = np.where(same_class, iou, 0.0)
    if iou.size == 0:
        return [], list(range(len(tracks_boxes))), list(range(len(det_boxes)))
    rows, cols = linear_sum_assignment(-iou)
    pairs = [(r, c) for r, c in zip(rows, cols) if iou[r, c] >= min_iou]
    mt = {r for r, _ in pairs}
    md = {c for _, c in pairs}
    return (pairs,
            [i for i in range(len(tracks_boxes)) if i not in mt],
            [j for j in range(len(det_boxes)) if j not in md])


class Kalman:
    """Constant-velocity Kalman filter on [cx, cy, w, h] with variable dt (seconds)."""

    def __init__(self, box):
        cx, cy, w, h = _xyxy_to_cxcywh(box)
        self.x = np.array([cx, cy, w, h, 0, 0, 0, 0], dtype=np.float64)
        s = max(h, 1.0)
        self.P = np.diag([(0.1 * s) ** 2] * 4 + [(1.0 * s) ** 2] * 4)
        self.H = np.eye(4, 8)

    def predict(self, dt):
        F = np.eye(8)
        F[:4, 4:] = np.eye(4) * dt
        s = max(self.x[3], 1.0)
        # Noise grows with target size and elapsed time
        q_pos, q_vel = (0.05 * s) ** 2 * dt, (0.5 * s) ** 2 * dt
        Q = np.diag([q_pos] * 4 + [q_vel] * 4)
        self.x = F @ self.x
        self.x[2:4] = np.maximum(self.x[2:4], 1.0)
        self.P = F @ self.P @ F.T + Q

    def update(self, box):
        z = np.array(_xyxy_to_cxcywh(box))
        s = max(self.x[3], 1.0)
        R = np.diag([(0.05 * s) ** 2] * 4)
        S = self.H @ self.P @ self.H.T + R
        K = self.P @ self.H.T @ np.linalg.inv(S)
        self.x = self.x + K @ (z - self.H @ self.x)
        self.P = (np.eye(8) - K @ self.H) @ self.P


def _xyxy_to_cxcywh(b):
    x1, y1, x2, y2 = b
    return (x1 + x2) / 2, (y1 + y2) / 2, max(x2 - x1, 1.0), max(y2 - y1, 1.0)


def _cxcywh_to_xyxy(cx, cy, w, h):
    return np.array([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2])


_ids = count(1)


@dataclass
class Track:
    box: np.ndarray                 # last detection box (x1,y1,x2,y2)
    score: float
    cls: int
    name: str
    t: float                        # time of last kalman state (s)
    kf: Kalman = None
    id: int = 0
    state: int = TENTATIVE
    hits: int = 1
    t_last_seen: float = 0.0
    t_start: float = 0.0
    prev_anchor: tuple = None       # anchor point before the latest update (for line crossing)
    anchor: tuple = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        self.kf = Kalman(self.box)
        self.t_last_seen = self.t_start = self.t
        self.anchor = _anchor(self.box)

    def predicted_box(self):
        return _cxcywh_to_xyxy(*self.kf.x[:4])


@dataclass(frozen=True)
class TrackView:
    """Immutable copy of a track for the drawing thread (tracker keeps mutating Track)."""
    id: int
    name: str
    score: float
    box: np.ndarray
    kx: np.ndarray       # kalman state copy
    t: float

    def box_at(self, t, max_extrapolate=0.5):
        dt = float(np.clip(t - self.t, 0.0, max_extrapolate))
        cx, cy, w, h, vx, vy, vw, vh = self.kx
        return _cxcywh_to_xyxy(cx + vx * dt, cy + vy * dt, max(w + vw * dt, 1.0), max(h + vh * dt, 1.0))


def views(tracks):
    return [TrackView(tr.id, tr.name, tr.score, tr.box.copy(), tr.kf.x.copy(), tr.t) for tr in tracks]


def _anchor(box):
    """Bottom-center point: where the target touches the ground. Used by zone/line rules."""
    return (float((box[0] + box[2]) / 2), float(box[3]))


class ByteTracker:
    def __init__(self, high_thresh=0.4, low_thresh=0.1, new_thresh=0.4,
                 match_iou=0.2, low_match_iou=0.5, tentative_iou=0.3,
                 confirm_hits=2, max_lost_sec=2.0):
        self.high = high_thresh
        self.low = low_thresh
        self.new = new_thresh
        self.match_iou = match_iou
        self.low_match_iou = low_match_iou
        self.tentative_iou = tentative_iou
        self.confirm_hits = confirm_hits
        self.max_lost_sec = max_lost_sec
        self.tracks = []
        self.removed = []           # ids removed in the last update (rules use this to clean up)

    def reset(self):
        self.tracks = []

    def update(self, dets, t):
        """dets: list of Detection (x1,y1,x2,y2,score,cls,name). t: frame time in seconds.
        Returns confirmed tracks updated in this frame."""
        for tr in self.tracks:
            tr.kf.predict(max(t - tr.t, 0.0))
            tr.t = t

        high = [d for d in dets if d.score >= self.high]
        low = [d for d in dets if self.low <= d.score < self.high]
        dbox = lambda ds: [[d.x1, d.y1, d.x2, d.y2] for d in ds]
        same_cls = lambda trs, ds: np.array([[tr.cls == d.cls for d in ds] for tr in trs], dtype=bool).reshape(len(trs), len(ds))

        active = [tr for tr in self.tracks if tr.state in (TRACKED, LOST)]
        tentative = [tr for tr in self.tracks if tr.state == TENTATIVE]

        # 1) confirmed + lost tracks  <->  high-score detections
        pairs, um_t, um_d = match([tr.predicted_box() for tr in active], dbox(high),
                                  self.match_iou, same_cls(active, high))
        for ti, di in pairs:
            self._apply(active[ti], high[di], t)
        remain_tracks = [active[i] for i in um_t]
        remain_high = [high[i] for i in um_d]

        # 2) still-unmatched TRACKED tracks  <->  low-score detections (occluded targets)
        r_tracked = [tr for tr in remain_tracks if tr.state == TRACKED]
        pairs, um_t2, _ = match([tr.predicted_box() for tr in r_tracked], dbox(low),
                                self.low_match_iou, same_cls(r_tracked, low))
        for ti, di in pairs:
            self._apply(r_tracked[ti], low[di], t)
        for i in um_t2:
            r_tracked[i].state = LOST
        # lost tracks that did not match stay LOST

        # 3) tentative tracks  <->  remaining high detections
        pairs, um_tt, um_d3 = match([tr.predicted_box() for tr in tentative], dbox(remain_high),
                                    self.tentative_iou, same_cls(tentative, remain_high))
        for ti, di in pairs:
            tr = tentative[ti]
            self._apply(tr, remain_high[di], t)
            if tr.hits >= self.confirm_hits:
                tr.state = TRACKED
                tr.id = next(_ids)
        dead = {id(tentative[i]) for i in um_tt}   # tentative miss once -> drop

        # 4) new tentative tracks from unmatched confident detections
        for i in um_d3:
            d = remain_high[i]
            if d.score >= self.new:
                self.tracks.append(Track(box=np.array([d.x1, d.y1, d.x2, d.y2]), score=d.score,
                                         cls=d.cls, name=d.name, t=t))

        # 5) drop dead tentative and long-lost tracks
        self.removed = [tr.id for tr in self.tracks
                        if tr.state == LOST and t - tr.t_last_seen > self.max_lost_sec]
        self.tracks = [tr for tr in self.tracks
                       if id(tr) not in dead
                       and not (tr.state == LOST and t - tr.t_last_seen > self.max_lost_sec)]
        return [tr for tr in self.tracks if tr.state == TRACKED]

    @staticmethod
    def _apply(tr, d, t):
        box = np.array([d.x1, d.y1, d.x2, d.y2])
        tr.kf.update(box)
        tr.box = box
        tr.score = d.score
        tr.hits += 1
        tr.t_last_seen = t
        tr.prev_anchor = tr.anchor
        tr.anchor = _anchor(box)
        if tr.state == LOST:
            tr.state = TRACKED
