"""Talks to the control service: rules + video features (hot reload), alarm upload, heartbeat.

All network I/O happens in background threads so the analysis loop never waits on HTTP.

UDP both ways on one socket: track boxes go to the API's port; the API answers on the same
socket with commands, so a switch flipped in the UI takes effect at once instead of at the next
poll: {"cmd": "reload"} (fetch rules + video features now), {"cmd": "keyframe"} (a viewer
starts playing). The socket is connected to the API address, so only it can send commands.
"""
import json
import logging
import os
import queue
import socket
import threading
import time
from dataclasses import asdict

import cv2
import httpx

log = logging.getLogger("api")


class ApiClient:
    def __init__(self, base, stream, poll_sec=5.0, heartbeat_sec=5.0):
        self.base = base.rstrip("/")
        self.stream = stream
        headers = {}
        if os.getenv("INTERNAL_TOKEN"):
            headers["X-Internal-Token"] = os.environ["INTERNAL_TOKEN"]
        # trust_env=False: local API must never go through an http proxy from env vars
        self.http = httpx.Client(timeout=5, trust_env=False, headers=headers)
        self.rules_version = None
        self.rules = None
        self.video = {}                 # video features (Device.video)
        self.on_video = None            # callback(cfg) when they change
        self.on_viewer = None           # callback() when the API says a viewer starts playing
        self._wake = threading.Event()
        self._q = queue.Queue(maxsize=100)
        self.stats_fn = None
        self.poll_sec = poll_sec
        self.heartbeat_sec = heartbeat_sec
        # Track boxes for browser-side overlays: UDP to the API's port (fire and forget; a lost
        # datagram is replaced by the next one 1/det_fps later, and a slow API never stalls us)
        u = httpx.URL(self.base)
        self._udp_addr = (u.host, u.port or (443 if u.scheme == "https" else 80))
        self._udp = socket.socket(socket.AF_INET6 if ":" in u.host else socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._udp.connect(self._udp_addr)
        except OSError as e:            # API host not resolvable yet: send_tracks just fails quietly
            log.warning("udp connect %s: %s", self._udp_addr, e)

    # ---- rules ----
    def fetch_rules(self):
        """Returns (version, rules, video features). Raises on network errors."""
        r = self.http.get(f"{self.base}/api/internal/rules/{self.stream}")
        r.raise_for_status()
        d = r.json()
        return d["version"], d["rules"], d.get("video") or {}

    def wait_rules(self):
        """Block until the API answers (worker is useless without rules)."""
        delay = 1
        while True:
            try:
                self.rules_version, self.rules, self.video = self.fetch_rules()
                log.info("rules v%s loaded: %d zones, %d lines", self.rules_version,
                         len(self.rules.get("zones", [])), len(self.rules.get("lines", [])))
                return self.rules
            except Exception as e:
                log.warning("fetch rules failed (%s), retry in %ds", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)

    # ---- alarms ----
    def emit(self, event, frame, clip=None):
        """Same interface as alarm.AlarmSink. Non-alarm events (line counts) go via heartbeat stats."""
        if event.alarm:
            self.send_alarm(event, frame, clip)

    def send_alarm(self, event, frame, clip=None):
        """Non-blocking: JPEG-encode now (frame may be reused), upload in background."""
        rec = asdict(event)
        rec["stream"] = self.stream
        jpg = None
        if frame is not None:
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            jpg = buf.tobytes() if ok else None
        try:
            self._q.put_nowait((rec, jpg, clip))
        except queue.Full:
            log.error("alarm queue full, dropping %s", rec["type"])
            if clip:
                clip.give_up()

    def _upload_loop(self):
        while True:
            rec, jpg, clip = self._q.get()
            files = {"snapshot": ("snap.jpg", jpg, "image/jpeg")} if jpg else None
            alarm_id = None
            for attempt in range(5):
                try:
                    r = self.http.post(f"{self.base}/api/internal/alarms",
                                       data={"event": json.dumps(rec, ensure_ascii=False)}, files=files)
                    r.raise_for_status()
                    alarm_id = r.json().get("id")
                    log.warning("ALARM %s rule=%s track=%s -> id %s", rec["type"], rec["rule_name"],
                                rec["track_id"], alarm_id)
                    break
                except Exception as e:
                    log.warning("upload alarm failed (%s), attempt %d", e, attempt + 1)
                    time.sleep(2 ** attempt)
            if clip:
                if alarm_id:
                    clip.add_id(alarm_id)
                else:
                    clip.give_up()

    def save_clip(self, job, mp4):
        """ClipRecorder callback (its thread): upload the clip once the alarm ids are known."""
        ids = job.wait_ids(timeout=60)
        if not ids:
            log.warning("clip dropped: no alarm id")
            return
        for attempt in range(3):
            try:
                with open(mp4, "rb") as f:
                    r = self.http.post(f"{self.base}/api/internal/clips", timeout=30,
                                       data={"alarm_ids": json.dumps(ids), "start": f"{job.start:.3f}",
                                             "end": f"{job.end:.3f}"},
                                       files={"clip": ("clip.mp4", f, "video/mp4")})
                r.raise_for_status()
                log.info("clip uploaded for alarms %s", ids)
                return
            except Exception as e:
                log.warning("upload clip failed (%s), attempt %d", e, attempt + 1)
                time.sleep(2 ** attempt)

    # ---- live track boxes ----
    def send_tracks(self, payload):
        try:
            data = json.dumps(payload, separators=(",", ":")).encode()
            if len(data) < 60000:
                self._udp.send(data)
        except OSError as e:
            log.debug("send tracks failed: %s", e)

    # ---- commands from the API (UDP) ----
    def _command_loop(self):
        while True:
            try:
                msg = json.loads(self._udp.recv(2048))
            except (OSError, ValueError):   # ICMP port unreachable while the API restarts, junk
                time.sleep(0.2)
                continue
            cmd = msg.get("cmd") if isinstance(msg, dict) else None
            if cmd == "reload":
                self._wake.set()
            elif cmd == "keyframe" and self.on_viewer:
                self.on_viewer()

    # ---- heartbeat + rules polling ----
    def _status_loop(self, on_rules_changed):
        last_poll = last_beat = time.monotonic()
        while True:
            woke = self._wake.wait(1.0)
            self._wake.clear()
            now = time.monotonic()
            if now - last_beat >= self.heartbeat_sec:
                last_beat = now
                try:
                    stats = self.stats_fn() if self.stats_fn else {}
                    self.http.post(f"{self.base}/api/internal/heartbeat/{self.stream}", json=stats)
                except Exception as e:
                    log.debug("heartbeat failed: %s", e)
            if woke or now - last_poll >= self.poll_sec:
                last_poll = now
                try:
                    version, rules, video = self.fetch_rules()
                    if version != self.rules_version:
                        log.info("rules changed v%s -> v%s, reloading", self.rules_version, version)
                        self.rules_version, self.rules = version, rules
                        on_rules_changed(rules)
                    if video != self.video:
                        self.video = video
                        if self.on_video:
                            self.on_video(video)
                except Exception as e:
                    log.debug("poll rules failed: %s", e)

    def start(self, on_rules_changed):
        threading.Thread(target=self._upload_loop, daemon=True, name="alarm-upload").start()
        threading.Thread(target=self._status_loop, args=(on_rules_changed,), daemon=True,
                         name="heartbeat").start()
        threading.Thread(target=self._command_loop, daemon=True, name="api-commands").start()
