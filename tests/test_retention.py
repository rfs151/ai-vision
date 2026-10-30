import os
import tempfile
import time

if "DATA_DIR" not in os.environ:          # never touch the real data dir / database
    _tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = _tmp
    os.environ["DB_URL"] = f"sqlite:///{_tmp}/test.db"

from api import retention  # noqa: E402
from api.config import settings  # noqa: E402
from api.db import SessionLocal, migrate  # noqa: E402
from api.models import Alarm, Device  # noqa: E402

DAY = 86400


def make(root, rel, size, age_days, now):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    os.utime(p, (now - age_days * DAY, now - age_days * DAY))
    return p


def test_pick_age_then_size():
    now = 1_000_000_000
    files = [(now - 10 * DAY, 100, "old"), (now - 2 * DAY, 100, "a"), (now - DAY, 100, "b"), (now, 100, "c")]
    assert [f[2] for f in retention.pick(files, 7, 0, now)] == ["old"]
    assert [f[2] for f in retention.pick(files, 7, 200, now)] == ["old", "a"]   # then oldest until <= 200
    assert retention.pick(files, 0, 0, now) == []                               # both limits off


def test_clean_deletes_files_and_clears_links():
    migrate()
    root = settings.data_dir
    now = time.time()
    old_jpg = make(root, "alarms/rt/20200101/000000_a.jpg", 10, 30, now)
    old_mp4 = make(root, "clips/rt/20200101/000000_1.mp4", 10, 30, now)
    new_mp4 = make(root, "clips/rt/20990101/000000_2.mp4", 10, 0, now)
    with SessionLocal() as db:
        dev = Device(name="rt", app="live", stream="rt")
        db.add(dev)
        db.flush()
        old = Alarm(device_id=dev.id, type="intrusion", rule_id="z", rule_name="z", track_id=1, cls="person", score=0.9, snapshot="alarms/rt/20200101/000000_a.jpg",
                    clip="clips/rt/20200101/000000_1.mp4")
        new = Alarm(device_id=dev.id, type="intrusion", rule_id="z", rule_name="z", track_id=1, cls="person", score=0.9, clip="clips/rt/20990101/000000_2.mp4")
        db.add_all([old, new])
        db.commit()
        ids = old.id, new.id

    n, freed = retention.clean(root, keep_days=7, max_gb=0, now=now)
    assert n == 2 and freed == 20
    assert not old_jpg.exists() and not old_mp4.exists() and new_mp4.exists()
    assert not (root / "clips/rt/20200101").exists()             # empty day folder removed
    with SessionLocal() as db:
        old, new = db.get(Alarm, ids[0]), db.get(Alarm, ids[1])
        assert old.snapshot is None and old.clip is None          # record kept, links cleared
        assert new.clip == "clips/rt/20990101/000000_2.mp4"


def test_clear_missing_links():
    migrate()
    with SessionLocal() as db:
        dev = Device(name="rm", app="live", stream="rm")
        db.add(dev)
        db.flush()
        a = Alarm(device_id=dev.id, type="dwell", rule_id="z", rule_name="z", track_id=1, cls="person",
                  score=0.9, clip="clips/rm/gone.mp4")
        db.add(a)
        db.commit()
        aid = a.id
    assert retention.clear_missing(settings.data_dir) >= 1
    with SessionLocal() as db:
        assert db.get(Alarm, aid).clip is None
