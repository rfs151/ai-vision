"""H.264 encoder inside the worker process (PyAV + libx264), pushing RTSP to ZLM.

Replaces the ffmpeg subprocess that read raw frames from a pipe. In-process, every frame can be
controlled on its own (force a keyframe, change the keyframe interval, attach ROI / SEI data),
which the video features build on; and the encoder's own numbers (bitrate, QP, keyframes, encode
time) are measured here instead of guessed.

Keyframes are placed by this class, not by x264: one every `gop_s` seconds of source time, plus
any requested with request_keyframe(). gop_s can change at any time (dynamic keyframe interval);
x264's own keyint is only a far-away safety net and scene-cut keyframes are off.

ROI (region of interest) encoding: encode(..., rois=[(x, y, w, h), ...]) sends the frame through an
FFmpeg filter graph with one `addroi` per region, which attaches ROI side data that libx264 turns
into a lower QP inside the regions. Under the bitrate cap, rate control raises QP elsewhere to pay
for it: bits move from the background to the targets. libx264 only reads ROIs with adaptive
quantization on, so AQ is on for every encode at a strength so low (0.05) that without ROIs the
output matches AQ off (measured: PSNR within 0.15 dB) and ROI can be switched at any time.

One encode, two outputs: the RTSP push and, optionally, 2 s MPEG-TS segments for alarm clips
(clip.py), same packets, no second encode. Segments can only start at a keyframe, so with a
longer gop_s they get longer too.
"""
import logging
import threading
import time
from collections import deque
from fractions import Fraction

import av

log = logging.getLogger("encoder")
QP2LAMBDA = 118                 # FFmpeg's FF_QP2LAMBDA: quality_stats hold QP * 118
ROI_QOFFSET = -0.4              # addroi qoffset (-1..1); libx264 maps it to about -10 QP
WINDOW_S = 20.0                 # stats window: long enough to hold two keyframes at a 10 s interval


class Encoder:
    def __init__(self, dst, w, h, fps, seg_pattern=None, seg_sec=2, seg_strftime=True, gop_s=2.0):
        self.fps = fps
        self.gop_s = gop_s
        self.tb = Fraction(1, 90000)
        self.t0 = self.last_pts = self.last_key_t = None
        if dst.startswith("rtsp://"):
            # 1200-byte RTP: ZLM forwards RTSP-pushed RTP to WebRTC players unchanged; ffmpeg's default
            # (1472) + WebRTC header extensions + SRTP exceeds a 1500 MTU and those packets get dropped.
            self.out = av.open(dst, "w", format="rtsp", options={"rtsp_transport": "tcp", "pkt_size": "1200"})
        else:                           # a file (tests)
            self.out = av.open(dst, "w")
        s = self.out.add_stream("libx264", rate=Fraction(fps).limit_denominator(1001))
        s.width, s.height, s.pix_fmt = w, h, "yuv420p"
        s.time_base = self.tb
        cc = s.codec_context
        cc.time_base = self.tb
        cc.gop_size = max(1, int(fps * 60))     # safety net only; encode() places the keyframes
        cc.max_b_frames = 0
        cc.bit_rate = 0
        # Same rate control as the old ffmpeg command. maxrate/bufsize cap keyframe size (~30 KB
        # instead of ~135 KB): WebRTC sends a frame as one UDP burst and bursts over ~90 packets got
        # dropped. scenecut=0: every keyframe is one we asked for. AQ: see ROI in the module doc.
        cc.options = {"preset": "ultrafast", "tune": "zerolatency", "crf": "23",
                      "maxrate": "1200k", "bufsize": "300k",
                      "x264-params": "scenecut=0:aq-mode=1:aq-strength=0.05"}
        self.stream = s
        # Opens the codec and copies its parameters (SPS/PPS extradata) to the stream; the RTSP
        # ANNOUNCE goes out here. The filter and the segment output below need those parameters.
        self.out.start_encoding()
        # SPS/PPS before every keyframe: the RTSP muxer asks for global headers, so x264 writes them
        # once only. A WebRTC player whose decoder restarts mid-stream (Windows Chrome) needs them.
        self.bsf = av.BitStreamFilterContext("dump_extra=freq=keyframe", s)
        self.seg = self.seg_stream = None
        if seg_pattern:
            self.seg = av.open(seg_pattern, "w", format="segment",
                               options={"segment_time": str(seg_sec), "segment_format": "mpegts",
                                        "strftime": "1" if seg_strftime else "0"})
            self.seg_stream = self.seg.add_stream_from_template(s)

        self.lock = threading.Lock()
        self.force_key = None           # reason: next frame becomes an IDR frame (any thread may set it)
        self._reason = None             # reason of the frame being encoded (for its packet record)
        self._graph = self._graph_rois = None   # ROI filter graph and the regions it was built for
        # One record per encoded frame over the last WINDOW_S seconds (wall clock), read by stats()
        # from the heartbeat thread: (time, bytes, keyframe reason or None, qp, encode seconds)
        self._hist = deque()
        self._keys_t = deque(maxlen=6)  # source times of the last keyframes (interval average)

    def request_keyframe(self, reason="request"):
        with self.lock:
            self.force_key = reason

    def encode(self, img, t, rois=None):
        """img: BGR ndarray; t: source time in seconds (frame pts); rois: [(x, y, w, h)] to encode
        at higher quality, or None (ROI off). Returns the packets written (for quality checks)."""
        if self.t0 is None:
            self.t0 = t
        frame = av.VideoFrame.from_ndarray(img, format="bgr24")
        pts = int(round((t - self.t0) / self.tb))
        # Source time must move forward (the muxer rejects anything else); a camera whose clock
        # jumps back, or a repeated pts, gets one frame interval after the previous one instead.
        if self.last_pts is not None and pts <= self.last_pts:
            pts = self.last_pts + int(round(1 / self.fps / self.tb))
        frame.pts = self.last_pts = pts
        frame.time_base = self.tb
        now = float(pts * self.tb)
        with self.lock:
            reason, self.force_key = self.force_key, None
        # Half a frame of slack so a 2 s GOP at 10 fps is exactly every 20 frames despite rounding
        if not reason and (self.last_key_t is None or now - self.last_key_t >= self.gop_s - 0.5 / self.fps):
            reason = "gop"
        if reason:
            frame.pict_type = av.video.frame.PictureType.I
            self.last_key_t = now
            self._keys_t.append(now)
        self._reason = reason
        t1 = time.perf_counter()
        if rois is not None:
            frame = self._roi_filter(frame, rois)
        packets = self.stream.encode(frame)
        return self._write(packets, time.perf_counter() - t1)

    def _roi_filter(self, frame, rois):
        """BGR -> yuv420p with ROI side data. Rebuilt when the regions change (~0.8 ms, 5x/s)."""
        rois = tuple(rois)
        if self._graph is None or rois != self._graph_rois:
            g = av.filter.Graph()
            chain = [g.add_buffer(width=frame.width, height=frame.height, format="bgr24", time_base=self.tb),
                     g.add("format", "yuv420p")]
            for x, y, w, h in rois:
                chain.append(g.add("addroi", f"x={x}:y={y}:w={w}:h={h}:qoffset={ROI_QOFFSET}"))
            chain.append(g.add("buffersink"))
            for a, b in zip(chain, chain[1:]):
                a.link_to(b)
            g.configure()
            self._graph, self._graph_rois = g, rois
        self._graph.push(frame)
        out = self._graph.pull()
        out.pict_type = frame.pict_type     # keep a forced keyframe
        return out

    def _write(self, packets, enc_s=0.0, flush=False):
        # zerolatency: one packet per frame, out right away. QP from x264 via quality_stats.
        qp = None
        for p in packets:
            if p.has_sidedata("quality_stats"):
                qp = int.from_bytes(bytes(p.get_sidedata("quality_stats"))[:4], "little") / QP2LAMBDA
        out = [q for p in packets for q in self.bsf.filter(p)]
        if flush:
            out += self.bsf.flush()
        if out:
            reason = (self._reason or "x264") if any(q.is_keyframe for q in out) else None
            with self.lock:
                self._hist.append((time.monotonic(), sum(q.size for q in out), reason, qp, enc_s))
        written = []
        for q in out:
            data = bytes(q)             # before mux: the muxer may take the packet's buffer
            written.append((data, q.pts, q.is_keyframe))
            if self.seg:
                c = av.Packet(data)
                c.pts, c.dts, c.time_base = q.pts, q.dts, q.time_base
                c.is_keyframe = q.is_keyframe
                c.stream = self.seg_stream
                self.seg.mux(c)
            q.stream = self.stream
            self.out.mux(q)
        return written                  # [(bytes, pts, keyframe)]

    def stats(self):
        """Encoder numbers over the last WINDOW_S seconds (sliding, so a 10 s keyframe interval
        does not make the bitrate jump between heartbeats with and without a keyframe)."""
        now = time.monotonic()
        with self.lock:
            while self._hist and now - self._hist[0][0] > WINDOW_S:
                self._hist.popleft()
            h = list(self._hist)
        if not h:
            return {"out_kbps": 0, "qp": None, "enc_ms": None, "keyframes": 0, "key_interval_s": None,
                    "key_kb": None, "frame_kb": None, "gop_s": self.gop_s, "key_reasons": {}}
        span = max(1.0, min(WINDOW_S, now - h[0][0] + 1 / self.fps))
        keys = [x for x in h if x[2]]
        qps = [x[3] for x in h if x[3] is not None]
        enc = [x[4] for x in h if x[4]]
        reasons = {}
        for x in keys:
            reasons[x[2]] = reasons.get(x[2], 0) + 1
        kt = list(self._keys_t)
        return {
            "out_kbps": round(sum(x[1] for x in h) * 8 / span / 1000),
            "qp": round(sum(qps) / len(qps), 1) if qps else None,
            "enc_ms": round(sum(enc) / len(enc) * 1000, 2) if enc else None,
            "keyframes": len(keys),
            # Average of the last few intervals between keyframes (source time)
            "key_interval_s": round((kt[-1] - kt[0]) / (len(kt) - 1), 1) if len(kt) > 1 else None,
            "key_kb": round(sum(x[1] for x in keys) / len(keys) / 1024, 1) if keys else None,
            "frame_kb": round(sum(x[1] for x in h) / len(h) / 1024, 1),
            "gop_s": self.gop_s,
            "key_reasons": reasons,
        }

    def close(self):
        try:
            self._write(self.stream.encode(None), flush=True)
        except Exception as e:
            log.debug("flush: %s", e)
        for c in (self.out, self.seg):
            if c:
                try:
                    c.close()
                except Exception as e:
                    log.debug("close: %s", e)
