"""摔倒检测 (fall detection) on top of pose tracks (yolo11n-pose keypoints + ByteTrack ids).

Each detection gets a posture label from its box shape and its torso (shoulders -> hips):
    stand   box tall and narrow (width / height <= 0.6), torso roughly vertical
    lying   box wider than tall (width / height >= 1.0), or torso tilted >= 60 deg in a box that is
            not tall (>= 0.8): someone lying across the picture
    bend    anything in between: crouching, bending down, sitting, the middle of a fall

A fall = a person goes from standing to lying FAST (within window_sec) and stays down for
confirm_sec. Both conditions matter:
    - fast: lying down on a bed or the floor on purpose takes longer than ~3 s
    - stays down: someone who trips and gets straight up is not an emergency, and one odd frame
      (a box squashed by occlusion) never alarms
The impact often blurs the person for a few frames, the tracker loses them and the person on the
floor gets a NEW track id with no standing history. So a new track inherits the last standing time
of a track that disappeared at the same spot (within HANDOFF_SEC) just before.

Tuned on the UR Fall Detection Dataset (front camera): standing 0.3-0.5, crouching / bending /
sitting 0.6-0.9, lying 1.1-3.3. Keypoints are often unreliable in small or blurred people, so the box
shape is the main signal and the torso angle a second one.

Config (rules JSON, "fall" key, used only by workers running a pose model):
    {"enabled": true, "confirm_sec": 2, "window_sec": 3}
"""
import math
from dataclasses import dataclass

STAND_AR, LYING_AR = 0.6, 1.0
KPT_CONF = 0.4
HANDOFF_SEC = 1.5
# COCO keypoint ids
L_SHOULDER, R_SHOULDER, L_HIP, R_HIP = 5, 6, 11, 12


def torso_angle(kpts):
    """Degrees between the hips -> shoulders line and straight up: 0 = upright, 90 = lying,
    > 90 = head below hips. None when a shoulder or a hip is not confidently seen."""
    if kpts is None:
        return None
    sh = [kpts[i] for i in (L_SHOULDER, R_SHOULDER) if kpts[i][2] >= KPT_CONF]
    hp = [kpts[i] for i in (L_HIP, R_HIP) if kpts[i][2] >= KPT_CONF]
    if not sh or not hp:
        return None
    sx, sy = sum(k[0] for k in sh) / len(sh), sum(k[1] for k in sh) / len(sh)
    hx, hy = sum(k[0] for k in hp) / len(hp), sum(k[1] for k in hp) / len(hp)
    return math.degrees(math.atan2(abs(sx - hx), hy - sy))   # image y grows downward


def posture(box, kpts=None):
    x1, y1, x2, y2 = box
    ar = (x2 - x1) / max(y2 - y1, 1.0)
    angle = torso_angle(kpts)
    if ar >= LYING_AR or (angle is not None and angle >= 60 and ar >= 0.8):
        return "lying"
    if ar <= STAND_AR and (angle is None or angle <= 35):
        return "stand"
    return "bend"


@dataclass
class _State:
    last_stand: float = None    # last time seen standing
    down_since: float = None    # start of the current lying period
    last_down: float = None     # last time seen lying
    fast: bool = False          # this lying period started within window_sec of standing
    fired: bool = False         # alarmed for this fall (cleared when the person stands up again)
    seen: float = None          # last update time
    box: tuple = None           # box at that time


class FallRule:
    id, name = "fall", "摔倒检测"

    def __init__(self, cfg):
        cfg = cfg or {}
        self.enabled = cfg.get("enabled", True)
        self.confirm_sec = float(cfg.get("confirm_sec", 2))
        self.window_sec = float(cfg.get("window_sec", 3))
        self.states = {}            # track id -> _State
        self.count = 0              # falls since start (stats)

    def reset(self):
        self.states.clear()

    def update(self, tracks, t, removed_ids=()):
        """Labels every track (tr.extra["pose"]) and returns the tracks that just fell."""
        fallen = []
        for tr in tracks:
            st = self.states.get(tr.id)
            if st is None:
                st = self.states[tr.id] = _State(last_stand=self._handoff(tr.box, t))
            st.seen, st.box = t, tuple(tr.box)
            p = posture(tr.box, tr.kpts)
            if p == "stand":
                st.last_stand, st.down_since, st.fast, st.fired = t, None, False, False
            elif p == "lying":
                if st.down_since is None:
                    st.down_since = t
                    st.fast = st.last_stand is not None and t - st.last_stand <= self.window_sec
                st.last_down = t
                if self.enabled and st.fast and not st.fired and t - st.down_since >= self.confirm_sec:
                    st.fired = True
                    self.count += 1
                    fallen.append(tr)
            elif st.down_since is not None and t - st.last_down > 1.0:
                st.down_since, st.fast = None, False   # got up to a crouch / sitting: not down any more
            tr.extra["pose"] = "FALL" if st.fired else p
        for rid in removed_ids:
            self.states.pop(rid, None)
        return fallen

    def _handoff(self, box, t):
        """Last standing time of a track lost just before at this spot (the same person), else None."""
        cx = (box[0] + box[2]) / 2
        best = None
        for st in self.states.values():
            if st.seen is None or st.seen >= t or t - st.seen > HANDOFF_SEC or st.last_stand is None:
                continue
            x1, _, x2, _ = st.box
            w = x2 - x1
            if x1 - w <= cx <= x2 + w and (best is None or st.last_stand > best):
                best = st.last_stand
        return best
