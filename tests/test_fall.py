from types import SimpleNamespace

import numpy as np

from fall import FallRule, posture, torso_angle
from rules import RuleEngine

STAND = (100, 100, 150, 300)    # 50 x 200: width / height 0.25
BEND = (100, 200, 250, 380)     # 150 x 180: 0.83
LYING = (100, 300, 300, 380)    # 200 x 80: 2.5


def tr(tid, box):
    return SimpleNamespace(id=tid, name="person", score=0.9, box=np.array(box, dtype=float),
                           kpts=None, extra={}, anchor=((box[0] + box[2]) / 2, box[3]), prev_anchor=None)


def run(rule, frames, fps=5):
    """frames: list of (track id, box) per step. Returns [(time, track id)] of falls."""
    out = []
    for i, items in enumerate(frames):
        t = i / fps
        for x in rule.update([tr(tid, box) for tid, box in items], t):
            out.append((round(t, 1), x.id))
    return out


def kpts(shoulder, hip):
    k = np.zeros((17, 3))
    k[5] = k[6] = (*shoulder, 0.9)
    k[11] = k[12] = (*hip, 0.9)
    return k


def test_posture_from_box_shape():
    assert posture(STAND) == "stand"
    assert posture(BEND) == "bend"
    assert posture(LYING) == "lying"


def test_torso_angle_and_tilted_torso_counts_as_lying():
    assert torso_angle(kpts((0, 0), (0, 100))) < 1            # shoulders straight above hips
    assert 89 < torso_angle(kpts((100, 50), (0, 50))) < 91     # horizontal
    assert torso_angle(None) is None
    # box only 0.83 (bend), but the torso is horizontal: lying
    assert posture(BEND, kpts((240, 290), (120, 300))) == "lying"
    # tall box, torso tilted 41 deg (leaning): not standing
    assert posture(STAND, kpts((170, 120), (100, 200))) == "bend"


def test_fast_fall_alarms_once_after_confirm():
    frames = [[(1, STAND)]] * 5 + [[(1, BEND)]] * 2 + [[(1, LYING)]] * 20
    assert run(FallRule({}), frames) == [(3.4, 1)]           # down at 1.4 s + 2 s confirm


def test_slow_lie_down_does_not_alarm():
    # 4 s crouching / sitting between standing and lying (window_sec is 3)
    frames = [[(1, STAND)]] * 5 + [[(1, BEND)]] * 20 + [[(1, LYING)]] * 20
    assert run(FallRule({}), frames) == []


def test_getting_up_quickly_does_not_alarm():
    frames = ([[(1, STAND)]] * 5 + [[(1, LYING)]] * 5 + [[(1, STAND)]] * 5) * 2
    assert run(FallRule({}), frames) == []


def test_falls_again_after_standing_up():
    fall = [[(1, STAND)]] * 5 + [[(1, LYING)]] * 15
    assert [tid for _, tid in run(FallRule({}), fall * 2)] == [1, 1]


def test_new_track_id_after_impact_inherits_standing_time():
    # track 1 stands, is lost for 3 frames during the fall, track 2 appears on the floor below it
    frames = [[(1, STAND)]] * 5 + [[]] * 3 + [[(2, LYING)]] * 15
    assert run(FallRule({}), frames) == [(3.6, 2)]


def test_lying_track_far_away_does_not_inherit():
    far = (700, 300, 900, 380)
    frames = [[(1, STAND)]] * 5 + [[]] * 3 + [[(2, far)]] * 15
    assert run(FallRule({}), frames) == []


def test_disabled_labels_but_never_alarms():
    rule = FallRule({"enabled": False})
    frames = [[(1, STAND)]] * 5 + [[(1, LYING)]] * 20
    assert run(rule, frames) == []


def falls_of(e, people, t=0.0):
    """people fall one after another (4 s each). Returns (fall track ids, end time)."""
    events = []
    for tid in people:
        for box in [STAND] * 5 + [LYING] * 15:
            events += e.update([tr(tid, box)], t)
            t += 0.2
    assert all((ev.type, ev.rule_name) == ("fall", "摔倒检测") for ev in events)
    return [ev.track_id for ev in events], t


def test_rule_engine_fall_events_respect_cooldown():
    e = RuleEngine({"cooldown_sec": 600, "fall": {"confirm_sec": 1}}, 1000, 1000, pose=True)
    ids, t = falls_of(e, (1, 2, 3))
    assert ids == [1]                   # 2 and 3 fall within 600 s: suppressed
    assert e.fall.count == 3            # still counted in the stats
    ids, _ = falls_of(e, (4,), t + 600)
    assert ids == [4]                   # cooldown over


def test_rule_engine_fall_events_without_cooldown():
    e = RuleEngine({"cooldown_sec": 0, "fall": {"confirm_sec": 1}}, 1000, 1000, pose=True)
    assert falls_of(e, (1, 2))[0] == [1, 2]


def test_detect_workers_have_no_fall_rule():
    e = RuleEngine({"fall": {}}, 1000, 1000)
    assert e.fall is None
    assert e.update([tr(1, LYING)], 0.0) == []
