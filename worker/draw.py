"""Drawing helpers: tracks, zones, lines, status bar, alarm banner.

Every draw_* function draws on either a BGR ndarray (the default pipeline) or a Yuv420 picture
(yuv_direct: the decoded frame is drawn on as is and goes to the encoder without any BGR round
trip). On Yuv420 each shape is drawn three times: on the Y plane, and at half the coordinates on
the U and V planes (cv2's `shift=1` = coordinates in half pixels; text has no shift, so it is
drawn at half size). Colors stay BGR in this module and are converted per plane.
"""
from functools import lru_cache

import av
import cv2
import numpy as np

FONT = cv2.FONT_HERSHEY_SIMPLEX
ALARM_RED = (0, 0, 255)
# COCO skeleton: head, arms, torso, legs (keypoint id pairs)
SKELETON = [(0, 1), (0, 2), (1, 3), (2, 4), (5, 6), (5, 7), (7, 9), (6, 8), (8, 10),
            (5, 11), (6, 12), (11, 12), (11, 13), (13, 15), (12, 14), (14, 16)]
KPT_CONF = 0.4


class Yuv420:
    """A yuv420p picture in one contiguous array (Y, then U, then V), with plane views to draw on.
    Width and height must be even."""

    def __init__(self, arr, w, h):
        self.arr, self.w, self.h = arr, w, h
        flat, n, q = arr.reshape(-1), w * h, (w // 2) * (h // 2)
        self.y = flat[:n].reshape(h, w)
        self.u = flat[n:n + q].reshape(h // 2, w // 2)
        self.v = flat[n + q:n + 2 * q].reshape(h // 2, w // 2)
        self.shape = (h, w, 3)

    @classmethod
    def from_frame(cls, frame):
        """Copy a decoded yuv420p av.VideoFrame (planes only, no color conversion)."""
        return cls(frame.to_ndarray(format="yuv420p"), frame.width, frame.height)

    def copy(self):
        return Yuv420(self.arr.copy(), self.w, self.h)

    def to_frame(self):
        return av.VideoFrame.from_ndarray(self.arr.reshape(self.h * 3 // 2, self.w), format="yuv420p")


@lru_cache(maxsize=256)
def _yuv(color):
    """BGR -> (Y, U, V), BT.601 limited range: what libswscale uses for bgr24 <-> yuv420p."""
    b, g, r = color
    return (int(round(16 + (65.481 * r + 128.553 * g + 24.966 * b) / 255)),
            int(round(128 + (-37.797 * r - 74.203 * g + 112.0 * b) / 255)),
            int(round(128 + (112.0 * r - 93.786 * g - 18.214 * b) / 255)))


def _planes(img, color, thickness):
    """(plane, color, thickness, shift) to draw a shape on: the image itself for BGR, three planes
    for Yuv420 (chroma: half coordinates via shift=1, half line width)."""
    if not isinstance(img, Yuv420):
        return [(img, color, thickness, 0)]
    y, u, v = _yuv(color)
    tc = thickness if thickness < 0 else max(1, thickness // 2)
    return [(img.y, y, thickness, 0), (img.u, u, tc, 1), (img.v, v, tc, 1)]


def _rect(img, p1, p2, color, thickness):
    for p, c, th, s in _planes(img, color, thickness):
        cv2.rectangle(p, p1, p2, c, th, cv2.LINE_8, s)


def _line(img, p1, p2, color, thickness, line_type=cv2.LINE_8):
    for p, c, th, s in _planes(img, color, thickness):
        cv2.line(p, p1, p2, c, th, line_type, s)


def _arrow(img, p1, p2, color, thickness, tip):
    for p, c, th, s in _planes(img, color, thickness):
        cv2.arrowedLine(p, p1, p2, c, th, cv2.LINE_8, s, tip)


def _circle(img, center, radius, color, thickness, line_type=cv2.LINE_8):
    for p, c, th, s in _planes(img, color, thickness):
        cv2.circle(p, center, radius, c, th, line_type, s)


def _polylines(img, pts, color, thickness):
    for p, c, th, s in _planes(img, color, thickness):
        cv2.polylines(p, pts, True, c, th, cv2.LINE_8, s)


def _fillpoly(img, pts, color):
    for p, c, _, s in _planes(img, color, -1):
        cv2.fillPoly(p, pts, c, cv2.LINE_8, s)


def _text(img, text, org, scale, color, thickness):
    if not isinstance(img, Yuv420):
        cv2.putText(img, text, org, FONT, scale, color, thickness, cv2.LINE_AA)
        return
    # Glyphs are too fine to draw again at half size on U / V (they would not line up with Y).
    # Render once into a full-size coverage mask over the text's box, blend Y with it, and U / V
    # with the mask averaged 2x2: what BGR drawing + 4:2:0 conversion produces.
    (tw, th), base = cv2.getTextSize(text, FONT, scale, thickness)
    pad = thickness + 1
    x0, y0 = max(0, (org[0] - pad) & ~1), max(0, (org[1] - th - pad) & ~1)
    x1, y1 = min(img.w, (org[0] + tw + pad + 1) & ~1), min(img.h, (org[1] + base + pad + 1) & ~1)
    if x1 <= x0 or y1 <= y0:
        return
    mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.putText(mask, text, (org[0] - x0, org[1] - y0), FONT, scale, 255, thickness, cv2.LINE_AA)
    a = mask.astype(np.float32) / 255
    ac = cv2.resize(a, ((x1 - x0) // 2, (y1 - y0) // 2), interpolation=cv2.INTER_AREA)
    cy, cu, cv = _yuv(color)
    for p, val, m, (px0, py0, px1, py1) in ((img.y, cy, a, (x0, y0, x1, y1)),
                                           (img.u, cu, ac, (x0 // 2, y0 // 2, x1 // 2, y1 // 2)),
                                           (img.v, cv, ac, (x0 // 2, y0 // 2, x1 // 2, y1 // 2))):
        roi = p[py0:py1, px0:px1]
        roi[:] = (roi * (1 - m) + val * m + 0.5).astype(np.uint8)


def _blend(overlay, img, alpha):
    """img = overlay * alpha + img * (1 - alpha), in place (per plane for Yuv420: the conversion
    is affine, so blending in YUV gives the same picture as blending in BGR)."""
    if isinstance(img, Yuv420):
        cv2.addWeighted(overlay.arr, alpha, img.arr, 1 - alpha, 0, dst=img.arr)
    else:
        cv2.addWeighted(overlay, alpha, img, 1 - alpha, 0, dst=img)


def track_color(tid):
    # Stable pseudo-random bright color per track id (BGR)
    rng = np.random.default_rng(tid * 7919)
    h = int(rng.integers(0, 180))
    hsv = np.uint8([[[h, 200, 255]]])
    return tuple(int(c) for c in cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0])


def _label(img, text, x, y, color):
    (tw, th), _ = cv2.getTextSize(text, FONT, 0.5, 1)
    y = max(y, th + 6)
    _rect(img, (x, y - th - 6), (x + tw + 4, y), color, -1)
    _text(img, text, (x + 2, y - 4), 0.5, (255, 255, 255), 1)


def draw_rules(img, engine, alarm_zone_ids=()):
    if engine is None:
        return
    overlay = img.copy()
    for z in engine.zones:
        color = ALARM_RED if z.id in alarm_zone_ids else (255, 160, 0)
        pts = z.poly.astype(np.int32)
        _fillpoly(overlay, [pts], color)
        _polylines(img, [pts], color, 2)
    _blend(overlay, img, 0.18)
    for z in engine.zones:
        x, y = z.poly.min(axis=0).astype(int)
        _label(img, f"{z.name} n={z.count}", int(x), int(y) + 18, (160, 100, 0))
    for l in engine.lines:
        p1, p2 = tuple(map(int, l.p1)), tuple(map(int, l.p2))
        _line(img, p1, p2, (0, 255, 255), 2)
        # IN arrow: left-hand normal of p1->p2 (see rules.Line)
        mid = np.array([(p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2])
        d = np.array([p2[0] - p1[0], p2[1] - p1[1]], dtype=float)
        n = np.array([-d[1], d[0]])  # left-hand normal (-dy, dx), image coords
        n = n / (np.linalg.norm(n) + 1e-6) * 40
        _arrow(img, tuple(mid.astype(int)), tuple((mid + n).astype(int)), (0, 255, 255), 2, 0.35)
        _label(img, f"{l.name} IN {l.n_in} / OUT {l.n_out}", p1[0] + 5, p1[1] + 20, (0, 140, 140))


def draw_skeleton(img, kpts, color):
    pts = [(int(x), int(y)) if c >= KPT_CONF else None for x, y, c in kpts]
    for a, b in SKELETON:
        if pts[a] and pts[b]:
            _line(img, pts[a], pts[b], color, 2, cv2.LINE_AA)
    for p in pts:
        if p:
            _circle(img, p, 3, (255, 255, 255), -1, cv2.LINE_AA)


def draw_tracks(img, tracks, t=None, alarm_track_ids=()):
    """tracks: list of Track or TrackView. If t is given, boxes are extrapolated to time t.
    Pose models: skeleton too, and the posture (stand / bend / lying / FALL) in the label."""
    for tr in tracks:
        box = tr.box_at(t) if t is not None else tr.box
        x1, y1, x2, y2 = (int(v) for v in box)
        pose = getattr(tr, "pose", None) or getattr(tr, "extra", {}).get("pose", "")
        alarm = tr.id in alarm_track_ids or pose == "FALL"
        color = ALARM_RED if alarm else track_color(tr.id)
        kpts = tr.kpts_at(t) if t is not None and hasattr(tr, "kpts_at") else getattr(tr, "kpts", None)
        if kpts is not None:
            draw_skeleton(img, kpts, color)
        _rect(img, (x1, y1), (x2, y2), color, 3 if alarm else 2)
        _label(img, f"#{tr.id} {pose}" if pose else f"#{tr.id} {tr.name} {tr.score:.2f}", x1, y1, color)


def draw_status(img, text):
    _text(img, text, (10, 24), 0.6, (0, 0, 0), 4)
    _text(img, text, (10, 24), 0.6, (0, 255, 255), 2)


def draw_banner(img, text):
    h, w = img.shape[:2]
    _rect(img, (0, h - 36), (w, h), ALARM_RED, -1)
    _text(img, text, (10, h - 11), 0.65, (255, 255, 255), 2)
