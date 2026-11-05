import subprocess
import sys
from pathlib import Path

import av

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import motion  # noqa: E402
from motion import MotionGate, motion_fraction  # noqa: E402


def clip(path, vf):
    """3 s test clip (lavfi), H.264 with P frames, like a camera stream."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=10:duration=3,{vf}",
                    "-c:v", "libx264", "-g", "100", "-bf", "0", "-pix_fmt", "yuv420p", str(path)], check=True)
    c = av.open(str(path))
    s = c.streams.video[0]
    s.codec_context.options = {"flags2": "+export_mvs"}
    return [motion_fraction(f) for f in c.decode(s)]


def test_motion_fraction_still_vs_moving(tmp_path):
    still = clip(tmp_path / "still.mp4", "trim=end_frame=1,loop=40:1:0,noise=alls=2:allf=t")
    moving = clip(tmp_path / "moving.mp4", "null")             # testsrc2 animates
    assert still[0] is None and moving[0] is None              # I frame: no vectors
    still, moving = [m for m in still[3:] if m is not None], [m for m in moving if m is not None]
    assert sum(still) / len(still) < motion.THRESHOLD           # noise only (about 0.6 %)
    assert min(moving) > motion.THRESHOLD * 5


def test_gate_skips_only_when_still_enabled_and_recent(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(motion.time, "monotonic", lambda: now[0])
    g = MotionGate()
    g.add(0.001)
    assert not g.skip(True, have_previous=False)               # nothing to reuse yet: infer
    for _ in range(3):
        now[0] += 0.2
        g.add(0.001)
        assert g.skip(True, True)                              # still: reuse
    now[0] += 0.2
    g.add(0.001)
    g.add(0.05)                                                # moving frames: average above
    assert not g.skip(True, True)
    now[0] += 0.2
    assert not g.skip(True, True)                              # no P frame since (unknown): infer
    for _ in range(9):                                         # still for 1.8 s: skipped
        now[0] += 0.2
        g.add(0.001)
        assert g.skip(True, True)
    now[0] += 0.2
    g.add(0.001)
    assert not g.skip(True, True)                              # MAX_SKIP_S reached: infer anyway
    now[0] += 0.2
    g.add(0.001)
    assert not g.skip(False, True)                             # feature off
    s = g.stats()
    assert s["skip_pct"] == round(12 / 17 * 100) and s["motion_pct"] is not None
