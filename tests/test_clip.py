import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
from clip import ClipRecorder  # noqa: E402


def make_segments(d, t0, n, seg=2):
    """n real 2 s MPEG-TS segments named like ffmpeg's strftime %s output."""
    for i in range(n):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seg}",
                        "-c:v", "libx264", "-g", "20", "-bf", "0", "-f", "mpegts", str(d / f"{t0 + i * seg}.ts")],
                       check=True)


def test_alarms_close_together_share_and_extend_a_job(tmp_path):
    r = ClipRecorder(tmp_path / "ring", on_clip=None, pre=10, post=10)
    a = r.request(1000)
    assert (a.start, a.end) == (990, 1010)
    assert r.request(1005) is a and a.end == 1015         # inside window: extended
    assert r.request(1030) is not a                       # outside: new job
    r.request(1015)
    assert a.end == 1025
    r.request(1025)
    assert a.end == 1030                                   # capped at MAX_LEN (40 s)


def test_cut_joins_covering_segments(tmp_path):
    r = ClipRecorder(tmp_path / "ring", on_clip=None, pre=3, post=3)
    t0 = 1_000_000
    make_segments(r.dir, t0, 6)                            # covers t0 .. t0+12
    job = r.request(t0 + 6)                                # window t0+3 .. t0+9 -> segments 2,4,6,8
    mp4 = r.cut(job)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp4)],
                         capture_output=True, text=True, check=True).stdout
    assert abs(float(out) - 8) < 0.3


def test_ids_wait_and_give_up(tmp_path):
    r = ClipRecorder(tmp_path / "ring", on_clip=None)
    job = r.request()
    job.expect()
    job.expect()
    job.add_id(7)
    job.give_up()                                          # second upload failed
    t = time.monotonic()
    assert job.wait_ids(5) == [7] and time.monotonic() - t < 1


def test_cleanup_keeps_segments_a_pending_job_needs(tmp_path):
    r = ClipRecorder(tmp_path / "ring", on_clip=None, pre=10, post=10, keep_sec=60)
    now = 2_000_000
    for s in (now - 200, now - 100, now - 30, now - 2):
        (r.dir / f"{s}.ts").write_bytes(b"x")
    r.request(now - 95)                                    # needs segments from now-105
    r.cleanup(now)
    assert sorted(int(p.stem) for p in r.dir.glob("*.ts")) == [now - 100, now - 30, now - 2]
