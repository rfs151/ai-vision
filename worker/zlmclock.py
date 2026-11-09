"""Frame timestamps the browser can see, so overlay boxes match the exact frame on screen.

The worker decodes rtsp://zlm/live/<stream>; browsers play the same stream over WebRTC or HTTP-fMP4.
To draw a box on the frame it was detected on, every detection result carries that frame's
timestamp on the browser's own clocks:

  r  RTP timestamp (90 kHz). ZLM forwards the source's RTP timestamps unchanged to every RTSP and
     WebRTC player, and the browser reports it for each shown frame
     (requestVideoFrameCallback -> metadata.rtpTimestamp). But ffmpeg subtracts the timestamp of
     the first packet it received (RTP-Info rtptime of its PLAY reply), so frame.pts starts near 0.
     ZLM starts every new player at the latest keyframe (GOP cache): a second RTSP session opened
     right next to ours starts at the same keyframe, and its PLAY reply says which timestamp that is.
     Probing before AND after av.open() and getting the same value proves no new keyframe arrived
     in between, so  r = (pts + rtptime) mod 2^32  exactly.
  z  HTTP-fMP4 media time (seconds). ZLM re-stamps fMP4 on its own clock; <video>.currentTime /
     metadata.mediaTime use it. An fMP4 request starts at the same latest keyframe, so an RTSP probe,
     an fMP4 probe, an RTSP probe again (same value twice = same keyframe) pairs one RTP timestamp
     with one fMP4 time. Re-paired every PAIR_SEC in the background in case ZLM's clock jumps.

Everything is best effort: a failed probe only means no r / z, and the browser falls back to
estimating the delay.
"""
import logging
import re
import socket
import struct
import threading
import time
import urllib.request
from urllib.parse import urlparse

log = logging.getLogger("zlmclock")
PAIR_SEC = 30
TIMEOUT = 3


def probe_rtsp(url):
    """RTP timestamp of the first video frame ZLM would send to a new RTSP player (the GOP start)."""
    u = urlparse(url)
    with socket.create_connection((u.hostname, u.port or 554), timeout=TIMEOUT) as s:
        seq = 0

        def req(method, target, extra=""):
            nonlocal seq
            seq += 1
            s.sendall(f"{method} {target} RTSP/1.0\r\nCSeq: {seq}\r\n{extra}\r\n".encode())
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = s.recv(65536)
                if not chunk:
                    raise ConnectionError("closed")
                data += chunk
            head, body = data.split(b"\r\n\r\n", 1)
            head = head.decode(errors="replace")
            if " 200 " not in head.split("\r\n", 1)[0]:
                raise ConnectionError(head.split("\r\n", 1)[0])
            n = int((re.search(r"Content-Length:\s*(\d+)", head, re.I) or [0, 0])[1])
            while len(body) < n:
                body += s.recv(65536)
            return head, body.decode(errors="replace")

        head, sdp = req("DESCRIBE", url, "Accept: application/sdp\r\n")
        base = (re.search(r"Content-Base:\s*(\S+)", head, re.I) or [None, url])[1]
        m = re.search(r"m=video.*?a=control:(\S+)", sdp, re.S)
        if not m:
            raise ValueError("no video track")
        ctrl = m.group(1)
        track = ctrl if ctrl.startswith("rtsp") else base.rstrip("/") + "/" + ctrl
        head, _ = req("SETUP", track, "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n")
        session = re.search(r"Session:\s*([^;\r]+)", head).group(1)
        head, _ = req("PLAY", url, f"Session: {session}\r\n")
        m = re.search(r"rtptime=(\d+)", head)
        if not m:
            raise ValueError("no RTP-Info rtptime")
        return int(m.group(1))


def probe_fmp4(url):
    """fMP4 time (s) of the first video frame ZLM sends to a new HTTP-fMP4 player (the GOP start)."""
    with urllib.request.urlopen(urllib.request.Request(url), timeout=TIMEOUT) as r:
        buf, video_id, timescale = b"", None, {}
        while True:
            while len(buf) < 8:
                chunk = r.read(65536)
                if not chunk:
                    raise ConnectionError("fMP4 ended")
                buf += chunk
            size, kind = struct.unpack(">I4s", buf[:8])
            while len(buf) < size:
                chunk = r.read(65536)
                if not chunk:
                    raise ConnectionError("fMP4 ended")
                buf += chunk
            box, buf = buf[8:size], buf[size:]
            if kind == b"moov":
                video_id, timescale = _video_track(box)
            elif kind == b"moof" and video_id is not None:
                t = _first_tfdt(box, video_id)
                if t is not None:
                    return t / timescale


def _boxes(data):
    i = 0
    while i + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[i:i + 8])
        if size < 8:
            return
        yield kind, data[i + 8:i + size]
        i += size


def _video_track(moov):
    for kind, trak in _boxes(moov):
        if kind != b"trak":
            continue
        tid = handler = scale = None
        for k, b in _boxes(trak):
            if k == b"tkhd":
                tid = struct.unpack(">I", b[12:16] if b[0] == 0 else b[20:24])[0]
            elif k == b"mdia":
                for k2, b2 in _boxes(b):
                    if k2 == b"hdlr":
                        handler = b2[8:12]
                    elif k2 == b"mdhd":
                        scale = struct.unpack(">I", b2[12:16] if b2[0] == 0 else b2[20:24])[0]
        if handler == b"vide":
            return tid, scale
    raise ValueError("no video track in moov")


def _first_tfdt(moof, video_id):
    for kind, traf in _boxes(moof):
        if kind != b"traf":
            continue
        tid = tfdt = None
        for k, b in _boxes(traf):
            if k == b"tfhd":
                tid = struct.unpack(">I", b[4:8])[0]
            elif k == b"tfdt":
                tfdt = struct.unpack(">Q", b[4:12])[0] if b[0] == 1 else struct.unpack(">I", b[4:8])[0]
        if tid == video_id and tfdt is not None:
            return tfdt
    return None


class ZlmClock:
    """r / z timestamps for frames of one worker connection (see module doc)."""

    def __init__(self, rtsp_url, http_base=None):
        self.url = rtsp_url
        u = urlparse(rtsp_url)
        # rtsp://host/app/stream -> http://<ZLM http>/app/stream.live.mp4
        self.fmp4_url = f"{http_base.rstrip('/')}{u.path}.live.mp4" if http_base else None
        self.base = None            # RTP timestamp at pts 0 of the current connection
        self.pair = None            # (pts, fMP4 seconds) of one frame
        self._gen = 0

    def open(self, opener, tries=3):
        """opener() opens the PyAV container. Returns it, with self.base set when it could be proven."""
        self.base, self.pair = None, None
        self._gen += 1
        for i in range(tries):
            try:
                r1 = probe_rtsp(self.url)
            except Exception as e:
                log.warning("rtsp probe failed, no frame timestamps for the browser: %s", e)
                return opener()
            container = opener()
            try:
                r2 = probe_rtsp(self.url)
            except Exception as e:
                log.warning("rtsp probe failed: %s", e)
                return container
            if r1 == r2:
                self.base = r1
                log.info("rtp base %d", r1)
                if self.fmp4_url:
                    threading.Thread(target=self._pair_loop, args=(self._gen,), daemon=True).start()
                return container
            log.info("keyframe arrived during open (%d != %d), reopening", r1, r2)
            if i < tries - 1:
                container.close()
        log.warning("could not pin the rtp base, no frame timestamps for the browser")
        return container

    def stop(self):
        self._gen += 1

    def _pair_loop(self, gen):
        while gen == self._gen:
            try:
                r1 = probe_rtsp(self.url)
                z = probe_fmp4(self.fmp4_url)
                r2 = probe_rtsp(self.url)
                if r1 == r2 and gen == self._gen and self.base is not None:
                    self.pair = ((r1 - self.base) % 2 ** 32, z)
                    log.debug("fmp4 pair pts=%d z=%.3f", *self.pair)
            except Exception as e:
                log.info("fmp4 probe failed: %s", e)
            time.sleep(PAIR_SEC if self.pair else 3)

    def stamps(self, pts):
        """(r, z) for a frame pts (90 kHz), None where unknown."""
        if pts is None or self.base is None:
            return None, None
        r = (pts + self.base) % 2 ** 32
        pair = self.pair
        if not pair:
            return r, None
        d = ((pts % 2 ** 32) - pair[0] + 2 ** 31) % 2 ** 32 - 2 ** 31    # 32-bit wrap after ~13 h
        return r, round(pair[1] + d / 90000, 3)
