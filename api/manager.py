"""Starts / stops / supervises one AI worker process per stream.

- start(stream): spawn worker.py (live/<stream> -> ai/<stream>), rules fetched from this API
- stop(stream): terminate it
- supervise(): every few seconds restart crashed workers (exponential backoff) and
  restart hung ones (no heartbeat for HEARTBEAT_TIMEOUT seconds)
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
    state: str = "starting"          # starting|running|backoff|stopped

    def public(self):
        now = time.time()
        return {
            "stream": self.stream, "state": self.state,
            "pid": self.proc.pid if self.proc and self.proc.poll() is None else None,
            "uptime": round(now - self.started_at) if self.started_at else 0,
            "restarts": self.restarts,
            # max(0): a heartbeat may land between reading `now` and here (different threads)
            "heartbeat_age": round(max(0.0, now - self.last_heartbeat), 1) if self.last_heartbeat else None,
            "stats": self.stats,
        }


class WorkerManager:
    def __init__(self):
        self.workers: dict[str, WorkerInfo] = {}   # key: stream
        self._task = None
        self.log_dir = settings.data_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def build_args(self, device):
        src = f"{settings.zlm_rtsp}/{device.app}/{device.stream}"
        dst = f"{settings.zlm_rtsp}/{settings.ai_app}/{device.stream}"
        args = [settings.worker_python, settings.worker_script, "--src", src, "--dst", dst,
                "--name", device.stream, "--api", settings.api_url,
                "--det-fps", str(device.det_fps), "--classes", device.classes]
        return args + shlex.split(settings.worker_args)

    def start(self, device):
        w = self.workers.get(device.stream)
        if w and w.proc and w.proc.poll() is None:
            return w
        if not w and sum(1 for x in self.workers.values() if x.state != "stopped") >= settings.max_workers:
            log.warning("max_workers=%d reached, not starting %s", settings.max_workers, device.stream)
            return None
        w = w or WorkerInfo(device.app, device.stream, device.id, [])
        w.args = self.build_args(device)
        self.workers[device.stream] = w
        self._spawn(w)
        return w

    def _spawn(self, w):
        logf = open(self.log_dir / f"worker_{w.stream}.log", "ab")
        # New session: a Ctrl+C on the API does not hit workers directly; we stop them ourselves
        w.proc = subprocess.Popen(w.args, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        logf.close()
        w.started_at = time.time()
        w.last_heartbeat = 0.0
        w.state = "starting"
        log.info("worker %s started pid=%d", w.stream, w.proc.pid)

    def stop(self, stream, forget=False):
        w = self.workers.get(stream)
        if not w:
            return
        if w.proc and w.proc.poll() is None:
            w.proc.send_signal(signal.SIGTERM)
            try:
                w.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                w.proc.kill()
        w.state = "stopped"
        log.info("worker %s stopped", stream)
        if forget:
            self.workers.pop(stream, None)

    def restart(self, device):
        self.stop(device.stream)
        return self.start(device)

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

    async def supervise(self, interval=3.0):
        while True:
            await asyncio.sleep(interval)
            try:
                self._check()
            except Exception:
                log.exception("supervise failed")

    def _check(self):
        now = time.time()
        for w in self.workers.values():
            if w.state == "stopped":
                continue
            dead = w.proc is None or w.proc.poll() is not None
            hung = (not dead and now - w.started_at > settings.heartbeat_timeout
                    and now - max(w.last_heartbeat, w.started_at) > settings.heartbeat_timeout)
            if hung:
                log.warning("worker %s: no heartbeat for %.0fs, killing", w.stream, settings.heartbeat_timeout)
                w.proc.kill()
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

    def shutdown(self):
        for s in list(self.workers):
            self.stop(s)


manager = WorkerManager()
