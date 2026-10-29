"""Alarm rules on top of tracks: zone intrusion, dwell, crowd count, line crossing.

Rule config (JSON), coordinates normalized to 0..1 so they do not depend on resolution:

{
  "confirm_frames": 3,     # target must be inside for N consecutive detections (filters flicker)
  "cooldown_sec": 60,      # same rule alarms at most once per cooldown
  "zones": [{"id": "z1", "name": "restricted", "polygon": [[x,y],...],
             "classes": ["person"], "intrusion": true, "dwell_sec": 5, "max_count": 3}],
  "lines": [{"id": "l1", "name": "gate", "p1": [x,y], "p2": [x,y],
             "classes": ["person"], "alarm_on": "in" | "out" | "both" | null}]
}

Positions use the track anchor (bottom-center of the box = where it stands).
Line direction: walking toward the side the IN arrow points to (left-hand normal
of p1->p2 in image coordinates) counts as "in", the opposite as "out".
"""
from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class Event:
    type: str            # intrusion | dwell | crowd | line_in | line_out
    rule_id: str
    rule_name: str
    t: float             # frame time (s)
    track_id: int = 0
    cls: str = ""
    score: float = 0.0
    box: tuple = ()      # pixels x1,y1,x2,y2
    detail: dict = field(default_factory=dict)
    alarm: bool = True   # False = statistic only (e.g. line count without alarm_on)


def _side(p1, p2, pt):
    """>0 on one side of line p1->p2, <0 on the other (2D cross product)."""
    return (p2[0] - p1[0]) * (pt[1] - p1[1]) - (p2[1] - p1[1]) * (pt[0] - p1[0])


def _segments_intersect(a1, a2, b1, b2):
    d1, d2 = _side(b1, b2, a1), _side(b1, b2, a2)
    d3, d4 = _side(a1, a2, b1), _side(a1, a2, b2)
    return d1 * d2 < 0 and d3 * d4 < 0


class Zone:
    def __init__(self, cfg, w, h):
        self.id = cfg["id"]
        self.name = cfg.get("name", self.id)
        self.poly = np.array([[x * w, y * h] for x, y in cfg["polygon"]], dtype=np.float32)
        self.classes = set(cfg.get("classes") or [])
        self.intrusion = cfg.get("intrusion", True)
        self.dwell_sec = cfg.get("dwell_sec")
        self.max_count = cfg.get("max_count")
        # per track: consecutive inside count, time first confirmed inside, fired flags
        self.inside = {}       # track_id -> {"n": int, "since": float|None, "intr": bool, "dwell": bool}
        self.count = 0

    def contains(self, pt):
        return cv2.pointPolygonTest(self.poly, pt, False) >= 0


class Line:
    def __init__(self, cfg, w, h):
        self.id = cfg["id"]
        self.name = cfg.get("name", self.id)
        self.p1 = (cfg["p1"][0] * w, cfg["p1"][1] * h)
        self.p2 = (cfg["p2"][0] * w, cfg["p2"][1] * h)
        self.classes = set(cfg.get("classes") or [])
        self.alarm_on = cfg.get("alarm_on")
        self.n_in = 0
        self.n_out = 0
        self.crossed = {}      # track_id -> last direction, so jitter on the line is not double counted


class RuleEngine:
    def __init__(self, cfg, frame_w, frame_h):
        self.cfg = cfg
        self.confirm = cfg.get("confirm_frames", 3)
        self.cooldown = cfg.get("cooldown_sec", 60)
        self.zones = [Zone(z, frame_w, frame_h) for z in cfg.get("zones", [])]
        self.lines = [Line(l, frame_w, frame_h) for l in cfg.get("lines", [])]
        self._last_alarm = {}   # (rule_id, type) -> t

    def reset(self):
        """Call when the stream reconnects (timestamps restart)."""
        for z in self.zones:
            z.inside.clear()
            z.count = 0
        for l in self.lines:
            l.crossed.clear()
        self._last_alarm.clear()

    def update(self, tracks, t, removed_ids=()):
        """tracks: confirmed tracks updated at time t. Returns list of Event
        (alarm events already filtered by cooldown)."""
        events = []
        seen = {tr.id for tr in tracks}
        for z in self.zones:
            events += self._zone(z, tracks, seen, t)
        for l in self.lines:
            events += self._line(l, tracks, t)
        for rid in removed_ids:
            for z in self.zones:
                z.inside.pop(rid, None)
            for l in self.lines:
                l.crossed.pop(rid, None)
        return [e for e in events if not e.alarm or self._pass_cooldown(e)]

    def _pass_cooldown(self, e):
        key = (e.rule_id, e.type)
        last = self._last_alarm.get(key)
        if last is not None and e.t - last < self.cooldown:
            e.detail["suppressed"] = True
            return False
        self._last_alarm[key] = e.t
        return True

    def _zone(self, z, tracks, seen, t):
        events = []
        for tr in tracks:
            if z.classes and tr.name not in z.classes:
                continue
            st = z.inside.setdefault(tr.id, {"n": 0, "since": None, "intr": False, "dwell": False})
            if z.contains(tr.anchor):
                st["n"] += 1
                if st["n"] >= self.confirm and st["since"] is None:
                    st["since"] = t
                    if z.intrusion and not st["intr"]:
                        st["intr"] = True
                        events.append(self._ev("intrusion", z, t, tr))
                if (z.dwell_sec and st["since"] is not None and not st["dwell"]
                        and t - st["since"] >= z.dwell_sec):
                    st["dwell"] = True
                    events.append(self._ev("dwell", z, t, tr, {"seconds": round(t - st["since"], 1)}))
            else:
                # left the zone: start over (a later re-entry can alarm dwell again, not intrusion)
                st["n"], st["since"], st["dwell"] = 0, None, False
        # tracks not updated this frame (occluded / lost) keep their state; count only confirmed-inside
        z.count = sum(1 for tid, st in z.inside.items() if st["since"] is not None and tid in seen)
        if z.max_count is not None and z.count > z.max_count:
            events.append(Event("crowd", z.id, z.name, t, detail={"count": z.count, "max": z.max_count}))
        return events

    def _line(self, l, tracks, t):
        events = []
        for tr in tracks:
            if tr.prev_anchor is None or (l.classes and tr.name not in l.classes):
                continue
            if not _segments_intersect(tr.prev_anchor, tr.anchor, l.p1, l.p2):
                continue
            direction = "in" if _side(l.p1, l.p2, tr.anchor) > 0 else "out"
            if l.crossed.get(tr.id) == direction:
                continue   # same direction twice in a row = jitter around the line
            l.crossed[tr.id] = direction
            if direction == "in":
                l.n_in += 1
            else:
                l.n_out += 1
            alarm = l.alarm_on in ("both", direction)
            events.append(self._ev(f"line_{direction}", l, t, tr,
                                   {"in": l.n_in, "out": l.n_out}, alarm=alarm))
        return events

    @staticmethod
    def _ev(typ, rule, t, tr, detail=None, alarm=True):
        return Event(typ, rule.id, rule.name, t, tr.id, tr.name, round(tr.score, 2),
                     tuple(int(v) for v in tr.box), detail or {}, alarm)
