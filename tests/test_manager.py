import sys
import time
from types import SimpleNamespace

import pytest

from api.manager import WorkerInfo, WorkerManager

IGNORE_TERM = [sys.executable, "-c",
               "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"]
OBEY_TERM = [sys.executable, "-c", "import time; time.sleep(60)"]


def dev(stream, ai=True):
    return SimpleNamespace(id=1, app="live", stream=stream, ai_enabled=ai, det_fps=5, classes="person")


@pytest.fixture()
def m(monkeypatch):
    mgr = WorkerManager()
    spawned = []
    real_spawn = mgr._spawn

    def fake_spawn(w):
        w.args = OBEY_TERM            # never launch the real worker in tests
        spawned.append(w.stream)
        real_spawn(w)
    monkeypatch.setattr(mgr, "_spawn", fake_spawn)
    mgr.spawned = spawned
    yield mgr
    mgr.shutdown()


def test_stop_is_non_blocking_and_kills_after_grace(m):
    w = WorkerInfo("live", "s1", 1, IGNORE_TERM, state="stopped")
    m.workers["s1"] = w
    WorkerManager._spawn(m, w)        # real spawn with the SIGTERM-ignoring script
    t = time.monotonic()
    m.stop("s1")
    assert time.monotonic() - t < 0.5            # hook handlers must not wait
    assert w.state == "stopping" and w.alive()
    m._check()
    assert w.state == "stopping"                 # still in grace period
    w.stop_deadline = time.time() - 1
    m._check()                                    # SIGKILL
    w.proc.wait(timeout=5)
    m._check()
    assert w.state == "stopped"


def test_start_during_stopping_waits_for_old_process(m):
    d = dev("s2")
    m.start(d)
    first = m.workers["s2"].proc
    m.stop("s2")
    m.start(d)                                    # stream came back before the old worker exited
    w = m.workers["s2"]
    assert w.pending_start and w.proc is first and m.spawned == ["s2"]
    first.wait(timeout=5)
    m._check()
    assert m.spawned == ["s2", "s2"] and w.proc is not first and w.state == "starting"


def test_reconcile_starts_missing_and_stops_offline(m):
    a, b, c = dev("on"), dev("off"), dev("noai", ai=False)
    m.start(b)
    m.reconcile([a, b, c], online={("live", "on"), ("live", "noai")})
    assert "on" in m.spawned and "noai" not in m.spawned
    assert m.workers["off"].state == "stopping"
    m.reconcile([a, b, c], online={("live", "on"), ("live", "noai")})
    assert m.spawned.count("on") == 1             # idempotent


def test_reconcile_respects_manual_stop(m):
    d = dev("man")
    m.start(d)
    m.stop("man", manual=True)
    m.workers["man"].proc.wait(timeout=5)
    m._check()
    m.reconcile([d], online={("live", "man")})
    assert m.spawned == ["man"]                   # not restarted
    m.start(d)                                    # hook-driven start also respects it
    assert m.spawned == ["man"]
    m.start(d, manual=True)                       # user starts it again
    assert m.spawned == ["man", "man"]


def test_reconcile_forgets_deleted_devices(m):
    m.start(dev("gone"))
    m.reconcile([], online=set())
    assert "gone" not in m.workers
