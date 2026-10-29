"""Talks to the control service: rules (hot reload), alarm upload, heartbeat.

All network I/O happens in background threads so the analysis loop never waits on HTTP.
"""
import json
import logging
import os
import queue
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
        self._q = queue.Queue(maxsize=100)
        self.stats_fn = None
        self.poll_sec = poll_sec
        self.heartbeat_sec = heartbeat_sec

    # ---- rules ----
    def fetch_rules(self):
        """Returns (version, rules). Raises on network errors."""
        r = self.http.get(f"{self.base}/api/internal/rules/{self.stream}")
        r.raise_for_status()
        d = r.json()
        return d["version"], d["rules"]

    def wait_rules(self):
        """Block until the API answers (worker is useless without rules)."""
        delay = 1
        while True:
            try:
                self.rules_version, self.rules = self.fetch_rules()
                log.info("rules v%s loaded: %d zones, %d lines", self.rules_version,
                         len(self.rules.get("zones", [])), len(self.rules.get("lines", [])))
                return self.rules
            except Exception as e:
                log.warning("fetch rules failed (%s), retry in %ds", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)

    # ---- alarms ----
    def emit(self, event, frame):
        """Same interface as alarm.AlarmSink. Non-alarm events (line counts) go via heartbeat stats."""
        if event.alarm:
            self.send_alarm(event, frame)

    def send_alarm(self, event, frame):
        """Non-blocking: JPEG-encode now (frame may be reused), upload in background."""
        rec = asdict(event)
        rec["stream"] = self.stream
        jpg = None
        if frame is not None:
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            jpg = buf.tobytes() if ok else None
        try:
            self._q.put_nowait((rec, jpg))
        except queue.Full:
            log.error("alarm queue full, dropping %s", rec["type"])

    def _upload_loop(self):
        while True:
            rec, jpg = self._q.get()
            files = {"snapshot": ("snap.jpg", jpg, "image/jpeg")} if jpg else None
            for attempt in range(5):
                try:
                    r = self.http.post(f"{self.base}/api/internal/alarms",
                                       data={"event": json.dumps(rec, ensure_ascii=False)}, files=files)
                    r.raise_for_status()
                    log.warning("ALARM %s rule=%s track=%s -> id %s", rec["type"], rec["rule_name"],
                                rec["track_id"], r.json().get("id"))
                    break
                except Exception as e:
                    log.warning("upload alarm failed (%s), attempt %d", e, attempt + 1)
                    time.sleep(2 ** attempt)

    # ---- heartbeat + rules polling ----
    def _status_loop(self, on_rules_changed):
        last_poll = 0.0
        while True:
            time.sleep(self.heartbeat_sec)
            try:
                stats = self.stats_fn() if self.stats_fn else {}
                self.http.post(f"{self.base}/api/internal/heartbeat/{self.stream}", json=stats)
            except Exception as e:
                log.debug("heartbeat failed: %s", e)
            if time.monotonic() - last_poll >= self.poll_sec:
                last_poll = time.monotonic()
                try:
                    version, rules = self.fetch_rules()
                    if version != self.rules_version:
                        log.info("rules changed v%s -> v%s, reloading", self.rules_version, version)
                        self.rules_version, self.rules = version, rules
                        on_rules_changed(rules)
                except Exception as e:
                    log.debug("poll rules failed: %s", e)

    def start(self, on_rules_changed):
        threading.Thread(target=self._upload_loop, daemon=True, name="alarm-upload").start()
        threading.Thread(target=self._status_loop, args=(on_rules_changed,), daemon=True,
                         name="heartbeat").start()
