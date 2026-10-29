"""Drawing helpers: tracks, zones, lines, status bar, alarm banner."""
import cv2
import numpy as np

FONT = cv2.FONT_HERSHEY_SIMPLEX
ALARM_RED = (0, 0, 255)


def track_color(tid):
    # Stable pseudo-random bright color per track id (BGR)
    rng = np.random.default_rng(tid * 7919)
    h = int(rng.integers(0, 180))
    hsv = np.uint8([[[h, 200, 255]]])
    return tuple(int(c) for c in cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0])


def _label(img, text, x, y, color):
    (tw, th), _ = cv2.getTextSize(text, FONT, 0.5, 1)
    y = max(y, th + 6)
    cv2.rectangle(img, (x, y - th - 6), (x + tw + 4, y), color, -1)
    cv2.putText(img, text, (x + 2, y - 4), FONT, 0.5, (255, 255, 255), 1, cv2.LINE_AA)


def draw_rules(img, engine, alarm_zone_ids=()):
    if engine is None:
        return
    overlay = img.copy()
    for z in engine.zones:
        color = ALARM_RED if z.id in alarm_zone_ids else (255, 160, 0)
        pts = z.poly.astype(np.int32)
        cv2.fillPoly(overlay, [pts], color)
        cv2.polylines(img, [pts], True, color, 2)
    cv2.addWeighted(overlay, 0.18, img, 0.82, 0, dst=img)
    for z in engine.zones:
        x, y = z.poly.min(axis=0).astype(int)
        _label(img, f"{z.name} n={z.count}", int(x), int(y) + 18, (160, 100, 0))
    for l in engine.lines:
        p1, p2 = tuple(map(int, l.p1)), tuple(map(int, l.p2))
        cv2.line(img, p1, p2, (0, 255, 255), 2)
        # IN arrow: left-hand normal of p1->p2 (see rules.Line)
        mid = np.array([(p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2])
        d = np.array([p2[0] - p1[0], p2[1] - p1[1]], dtype=float)
        n = np.array([-d[1], d[0]])  # left-hand normal (-dy, dx), image coords
        n = n / (np.linalg.norm(n) + 1e-6) * 40
        cv2.arrowedLine(img, tuple(mid.astype(int)), tuple((mid + n).astype(int)), (0, 255, 255), 2, tipLength=0.35)
        _label(img, f"{l.name} IN {l.n_in} / OUT {l.n_out}", p1[0] + 5, p1[1] + 20, (0, 140, 140))


def draw_tracks(img, tracks, t=None, alarm_track_ids=()):
    """tracks: list of Track. If t is given, boxes are extrapolated to time t."""
    for tr in tracks:
        box = tr.box_at(t) if t is not None else tr.box
        x1, y1, x2, y2 = (int(v) for v in box)
        alarm = tr.id in alarm_track_ids
        color = ALARM_RED if alarm else track_color(tr.id)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 3 if alarm else 2)
        _label(img, f"#{tr.id} {tr.name} {tr.score:.2f}", x1, y1, color)


def draw_status(img, text):
    cv2.putText(img, text, (10, 24), FONT, 0.6, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, (10, 24), FONT, 0.6, (0, 255, 255), 2, cv2.LINE_AA)


def draw_banner(img, text):
    h, w = img.shape[:2]
    cv2.rectangle(img, (0, h - 36), (w, h), ALARM_RED, -1)
    cv2.putText(img, text, (10, h - 11), FONT, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
