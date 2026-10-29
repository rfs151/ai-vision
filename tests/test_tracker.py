import numpy as np

from detector import Detection
from tracker import ByteTracker, views

DT = 0.2  # 5 fps detection


def det(x, y, w=40, h=100, score=0.8, cls=0, name="person"):
    return Detection(x, y, x + w, y + h, score, cls, name)


def run(tracker, frames):
    out = []
    for i, dets in enumerate(frames):
        out.append(tracker.update(dets, i * DT))
    return out


def test_confirm_after_two_hits():
    tk = ByteTracker()
    assert tk.update([det(100, 100)], 0.0) == []          # tentative
    tracks = tk.update([det(105, 100)], DT)
    assert len(tracks) == 1 and tracks[0].id > 0


def test_id_stable_while_moving():
    tk = ByteTracker()
    frames = [[det(100 + 15 * i, 100)] for i in range(20)]   # 75 px/s to the right
    out = run(tk, frames)
    ids = {tr.id for tracks in out[1:] for tr in tracks}
    assert len(ids) == 1


def test_low_score_detection_keeps_track_alive():
    """Occlusion: score drops below high threshold; ByteTrack's 2nd round keeps the ID."""
    tk = ByteTracker()
    frames = [[det(100 + 10 * i, 100, score=0.8 if i < 5 or i > 8 else 0.2)] for i in range(12)]
    out = run(tk, frames)
    ids = [tr.id for tracks in out[1:] for tr in tracks]
    assert len(set(ids)) == 1
    assert all(len(t) == 1 for t in out[1:])               # never dropped


def test_short_gap_reuses_id_long_gap_does_not():
    tk = ByteTracker(max_lost_sec=2.0)
    first = run(tk, [[det(100, 100)]] * 3)[-1][0].id
    # 1 s gap (5 empty frames), same place -> same id
    for i in range(3, 8):
        tk.update([], i * DT)
    assert tk.update([det(100, 100)], 8 * DT)[0].id == first
    # 3 s gap -> removed; reappearing target gets a new id after confirmation
    for i in range(9, 24):
        tk.update([], i * DT)
    assert first in tk.removed or all(tr.id != first for tr in tk.tracks)
    tk.update([det(100, 100)], 24 * DT)
    new = tk.update([det(100, 100)], 25 * DT)[0].id
    assert new != first


def test_two_targets_distinct_ids_and_class_gating():
    tk = ByteTracker()
    frames = [[det(100 + 10 * i, 100), det(400 - 10 * i, 300, cls=2, name="car")] for i in range(8)]
    out = run(tk, frames)
    last = out[-1]
    assert len(last) == 2 and len({tr.id for tr in last}) == 2
    assert {tr.name for tr in last} == {"person", "car"}


def test_extrapolation_between_detections():
    """Velocity fills the gap: halfway to the next detection the box has moved on."""
    tk = ByteTracker()
    speed = 100.0  # px/s
    for i in range(10):
        tracks = tk.update([det(100 + speed * i * DT, 100)], i * DT)
    v = views(tracks)[0]
    t_last = 9 * DT
    x_now = v.box_at(t_last)[0]
    x_mid = v.box_at(t_last + DT / 2)[0]
    assert abs((x_mid - x_now) - speed * DT / 2) < 4      # within a few px of truth
    # extrapolation is capped (default 0.5 s)
    assert v.box_at(t_last + 10)[0] == v.box_at(t_last + 0.5)[0]
