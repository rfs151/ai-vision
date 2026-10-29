from types import SimpleNamespace

from rules import RuleEngine

W, H = 1000, 1000


def tr(tid, x, y, prev=None, name="person"):
    """Fake track: anchor is (x, y) in pixels."""
    return SimpleNamespace(id=tid, name=name, score=0.9, box=(x - 20, y - 80, x + 20, y),
                           anchor=(x, y), prev_anchor=prev)


def engine(**kw):
    cfg = {"confirm_frames": 3, "cooldown_sec": 60,
           "zones": [{"id": "z1", "name": "zone", "polygon": [[0.5, 0], [1, 0], [1, 1], [0.5, 1]],
                      "classes": ["person"], "intrusion": True, "dwell_sec": 5, "max_count": 1}],
           "lines": [{"id": "l1", "name": "gate", "p1": [0.3, 0], "p2": [0.3, 1],
                      "classes": ["person"], "alarm_on": "in"}]}
    cfg.update(kw)
    return RuleEngine(cfg, W, H)


def types(events):
    return [e.type for e in events]


def test_intrusion_needs_confirm_frames_and_fires_once():
    e = engine()
    assert e.update([tr(1, 700, 500)], 0.0) == []
    assert e.update([tr(1, 700, 500)], 0.2) == []
    assert types(e.update([tr(1, 700, 500)], 0.4)) == ["intrusion"]
    for i in range(3, 10):
        assert "intrusion" not in types(e.update([tr(1, 700, 500)], i * 0.2))


def test_flicker_does_not_alarm():
    e = engine()
    for i, x in enumerate([700, 100, 700, 100, 700, 100]):
        assert "intrusion" not in types(e.update([tr(1, x, 500)], i * 0.2))


def test_class_filter():
    e = engine()
    for i in range(5):
        assert e.update([tr(1, 700, 500, name="car")], i * 0.2) == []


def test_dwell_after_seconds():
    e = engine()
    got = []
    for i in range(40):                                   # 8 s inside
        got += types(e.update([tr(1, 700, 500)], i * 0.2))
    assert got.count("dwell") == 1
    assert got.index("dwell") > got.index("intrusion")


def test_cooldown_per_rule():
    e = engine()
    got = []
    for i in range(4):
        got += types(e.update([tr(1, 700, 500)], i * 0.2))
    for i in range(4, 8):                                 # another person, same rule, within 60 s
        got += types(e.update([tr(2, 800, 500)], i * 0.2))
    assert got.count("intrusion") == 1
    for i in range(400, 404):                             # after cooldown
        got += types(e.update([tr(3, 800, 500)], i * 0.2))
    assert got.count("intrusion") == 2


def test_crowd():
    e = engine()
    got = []
    for i in range(4):
        got += types(e.update([tr(1, 700, 500), tr(2, 800, 600)], i * 0.2))
    assert "crowd" in got


def test_line_in_out_counts_and_alarm_direction():
    e = engine()
    line = e.lines[0]  # x = 300, p1 top -> p2 bottom; "in" = toward left-hand normal = toward -x
    ev = e.update([tr(1, 290, 500, prev=(310, 500))], 0.0)  # right -> left: in
    assert types(ev) == ["line_in"] and ev[0].alarm
    ev = e.update([tr(2, 310, 500, prev=(290, 500))], 0.2)  # left -> right: out
    assert types(ev) == ["line_out"] and not ev[0].alarm      # statistic only
    assert (line.n_in, line.n_out) == (1, 1)


def test_line_jitter_not_double_counted():
    e = engine()
    xs = [310, 290, 310, 290]
    for i in range(1, len(xs)):
        e.update([tr(1, xs[i], 500, prev=(xs[i - 1], 500))], i * 0.2)
    line = e.lines[0]
    # in, out, in again -> direction alternates, each counted; but same direction twice is not
    assert (line.n_in, line.n_out) == (2, 1)
    e.update([tr(1, 290, 500, prev=(310, 500))], 1.0)       # "in" again right after "in"
    assert line.n_in == 2


def test_line_outside_segment_ignored():
    e = engine(lines=[{"id": "l1", "name": "short", "p1": [0.3, 0.0], "p2": [0.3, 0.2]}])
    assert e.update([tr(1, 290, 500, prev=(310, 500))], 0.0) == []
