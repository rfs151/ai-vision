"""Starts / stops / supervises one AI worker process per stream.

Two inputs drive it:
- ZLM hooks (edge-triggered, fast): start on stream online, stop on offline.
- reconcile() every few seconds (level-triggered, reliable): compares desired state
  (device online + ai_enabled + not stopped by hand) with running workers and fixes drift.
  Hooks can be lost (API restarting, ZLM hook timeout...), reconcile catches that.

Nothing here blocks: hook handlers must answer ZLM immediately, otherwise ZLM's
following hooks fail. stop() only sends SIGTERM; supervise() kills after a grace period.
"""
import asyncio
import logging
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass, field

from api.config import settings

log = logging.getLogger("manager")
STOP_GRACE_SEC = 5.0

# running-ish states: the manager owns the process lifecycle
ACTIVE = ("starting", "running", "backoff", "stopping")


@dataclass
class WorkerInfo:
    app: str
    stream: str
    device_id: int
    args: list
    proc: subprocess.Popen | None = None
    started_at: float = 0.0
    restarts: int = 0
    next_start: float = 0.0          # backoff: do not restart before this time
    last_heartbeat: float = 0.0
    stats: dict = field(default_factory=dict)
    state: str = "starting"          # starting|running|backoff|stopping|stopped
    stop_deadline: float = 0.0       # stopping: SIGKILL after this time
    pending_start: bool = False      # start requested while the old process is still stopping
    manual_stop: bool = False        # stopped by a user: reconcile must not restart it

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def public(self):
        now = time.time()
        return {
            "stream": self.stream, "state": self.state,
            "pid": self.proc.pid if self.alive() else None,
            "uptime": round(now - self.started_at) if self.started_at else 0,
            "restarts": self.restarts,
            # max(0): a heartbeat may land between reading `now` and here (different threads)
            "heartbeat_age": round(max(0.0, now - self.last_heartbeat), 1) if self.last_heartbeat else None,
            "manual_stop": self.manual_stop,
            "stats": self.stats,
        }


class WorkerManager:
    def __init__(self):
        self.workers: dict[str, WorkerInfo] = {}   # key: stream
        self._orphans: list[WorkerInfo] = []       # forgotten (device deleted) but still exiting
        self.log_dir = settings.data_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def build_args(self, device):
        src = f"{settings.zlm_rtsp}/{device.app}/{device.stream}"
        dst = f"{settings.zlm_rtsp}/{settings.ai_app}/{device.stream}"
        args = [settings.worker_python, settings.worker_script, "--src", src, "--dst", dst,
                "--name", device.stream, "--api", settings.api_url,
                "--det-fps", str(device.det_fps), "--classes", device.classes]
        return args + shlex.split(settings.worker_args)

    def is_active(self, stream):
        """Running or going to run (a stopping worker counts only if a restart is queued)."""
        w = self.workers.get(stream)
        return bool(w and w.state in ACTIVE and not (w.state == "stopping" and not w.pending_start))

    def start(self, device, manual=False):
        """Idempotent. manual=True clears a previous manual stop."""
        w = self.workers.get(device.stream)
        if w and w.manual_stop and not manual:
            return w
        if not w and sum(1 for x in self.workers.values() if x.state in ACTIVE) >= settings.max_workers:
            log.warning("max_workers=%d reached, not starting %s", settings.max_workers, device.stream)
            return None
        w = w or WorkerInfo(device.app, device.stream, device.id, [], state="stopped")
        self.workers[device.stream] = w
        w.args = self.build_args(device)
        w.manual_stop = False
        if w.state == "stopping":
            w.pending_start = True       # old process still exiting (it still holds ai/<stream>)
        elif w.state == "stopped" or (w.state == "backoff" and manual):
            self._spawn(w)
        # starting / running: already fine; backoff: supervise() restarts it when due
        return w

    def _spawn(self, w):
        logf = open(self.log_dir / f"worker_{w.stream}.log", "ab")
        # New session: a Ctrl+C on the API does not hit workers directly; we stop them ourselves
        w.proc = subprocess.Popen(w.args, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        logf.close()
        w.started_at = time.time()
        w.last_heartbeat = 0.0
        w.state = "starting"
        w.pending_start = False
        log.info("worker %s started pid=%d", w.stream, w.proc.pid)

    def stop(self, stream, forget=False, manual=False):
        """Non-blocking: SIGTERM now, SIGKILL from supervise() if still alive after the grace period."""
        w = self.workers.get(stream)
        if not w:
            return
        w.pending_start = False
        w.manual_stop = manual or w.manual_stop
        if w.alive():
            if w.state != "stopping":
                w.proc.send_signal(signal.SIGTERM)
                w.stop_deadline = time.time() + STOP_GRACE_SEC
                w.state = "stopping"
                log.info("worker %s stopping (pid %d)", stream, w.proc.pid)
        else:
            w.state = "stopped"
            log.info("worker %s stopped", stream)
        if forget:
            self.workers.pop(stream, None)
            if w.alive():
                self._orphans.append(w)

    def restart(self, device):
        self.stop(device.stream)
        return self.start(device, manual=True)

    def heartbeat(self, stream, stats):
        w = self.workers.get(stream)
        if not w:
            return False
        w.last_heartbeat = time.time()
        w.stats = stats
        if w.state == "starting":
            w.state = "running"
            w.restarts = 0 if time.time() - w.started_at > 60 else w.restarts
        return True

    def status(self, stream):
        w = self.workers.get(stream)
        return w.public() if w else None

    def all(self):
        return [w.public() for w in self.workers.values()]

    def reconcile(self, devices, online):
        """devices: all Device rows; online: set of (app, stream) currently in ZLM."""
        by_stream = {d.stream: d for d in devices}
        for d in devices:
            want = d.ai_enabled and (d.app, d.stream) in online
            w = self.workers.get(d.stream)
            if want and not self.is_active(d.stream) and not (w and w.manual_stop):
                log.warning("reconcile: %s is online but has no worker, starting", d.stream)
                self.start(d)
            elif not want and w and w.state in ACTIVE and w.state != "stopping":
                log.warning("reconcile: %s should not run (online=%s ai=%s), stopping",
                            d.stream, (d.app, d.stream) in online, d.ai_enabled)
                self.stop(d.stream)
        for s in [s for s in self.workers if s not in by_stream]:
            self.stop(s, forget=True)    # device deleted

    async def supervise(self, interval=3.0):
        while True:
            await asyncio.sleep(interval)
            try:
                self._check()
            except Exception:
                log.exception("supervise failed")

    def _check(self):
        now = time.time()
        self._orphans = [w for w in self._orphans if self._reap(w, now)]
        for w in self.workers.values():
            if w.state == "stopped":
                continue
            if w.state == "stopping":
                if self._reap(w, now):
                    continue
                w.state = "stopped"
                log.info("worker %s stopped", w.stream)
                if w.pending_start:
                    self._spawn(w)
                continue
            dead = not w.alive()
            hung = (not dead and now - w.started_at > settings.heartbeat_timeout
                    and now - max(w.last_heartbeat, w.started_at) > settings.heartbeat_timeout)
            if hung:
                log.warning("worker %s: no heartbeat for %.0fs, killing", w.stream, settings.heartbeat_timeout)
                w.proc.kill()
                w.proc.wait(timeout=5)
                dead = True
            if dead:
                if w.state != "backoff":
                    w.restarts += 1
                    delay = min(2 ** min(w.restarts, 5), 30)
                    w.next_start = now + delay
                    w.state = "backoff"
                    log.warning("worker %s exited (code %s), restart #%d in %ds",
                                w.stream, w.proc.returncode if w.proc else None, w.restarts, delay)
                elif now >= w.next_start:
                    self._spawn(w)

    @staticmethod
    def _reap(w, now):
        """For a stopping process: True while still alive (SIGKILL once past the deadline)."""
        if not w.alive():
            return False
        if now >= w.stop_deadline:
            log.warning("worker %s ignored SIGTERM for %.0fs, killing", w.stream, STOP_GRACE_SEC)
            w.proc.kill()
        return True

    def shutdown(self):
        """API exiting: stop everything and wait (the only blocking path)."""
        procs = [w.proc for w in list(self.workers.values()) + self._orphans if w.alive()]
        for p in procs:
            p.send_signal(signal.SIGTERM)
        deadline = time.time() + STOP_GRACE_SEC
        for p in procs:
            try:
                p.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                p.kill()


manager = WorkerManager()
