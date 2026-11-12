"""Video features, switched on per device in the web UI (Device.video) and applied frame by frame.

  dyn_gop  动态关键帧: a keyframe every GOP_LONG s instead of GOP_FIXED s, plus one right away
           when a viewer starts playing (so the long interval does not slow down start-up), when
           targets appear after a quiet spell and when an alarm fires.
  roi      ROI 编码: the target boxes (+20 % margin) are encoded at a lower QP; under the bitrate
           cap the background pays for it. See encoder.py.
  skip_static 画面静止时跳过推理: see motion.py (applied in worker.AnalysisThread).
  yuv_direct  YUV 直通: no BGR round trip. The decode loop draws on the decoded yuv420p planes
           (draw.Yuv420) and hands them to the encoder as they are; the analysis thread converts
           only the frames it detects on, scaled down to the detector size in the same pass.
           The default pipeline converts every frame to BGR and back (applied in worker.py).
  rect_input 矩形输入: see detector.py (applied in worker.AnalysisThread).
  int8     INT8 模型: the quantized <model>_int8_openvino_model (scripts/quantize_int8.py) instead of
           the FP32 one. With rect_input or int8 and compare on, detector.InferProbe also runs the
           baseline (FP32, square input) on the same frame now and then.
  compare  对比模式: a second encode of the same picture with every feature off (the baseline),
           pushed to ai_ref/<stream>, so the UI can play both side by side. Costs one more encode,
           plus decoding both outputs to measure their quality (QualityProbe).
"""
import logging
import threading
import time
from collections import deque

import av
import numpy as np

from encoder import Encoder

log = logging.getLogger("video")
GOP_FIXED = 2.0         # baseline (and what the worker always did)
GOP_LONG = 10.0
QUIET_SEC = 3.0         # no targets this long, then some: "activity" keyframe
REF_RETRY_SEC = 10.0
ROI_MARGIN = 0.2        # grow each box by 20 % per side: targets move between detections
ROI_MAX = 16            # largest boxes first
ROI_MIN_PX = 16


def roi_boxes(track_views, w, h):
    """Track boxes -> [(x, y, w, h)] regions for the encoder and for the quality measurement."""
    out = []
    for v in track_views:
        x1, y1, x2, y2 = v.box
        mx, my = (x2 - x1) * ROI_MARGIN, (y2 - y1) * ROI_MARGIN
        x1, y1 = max(0, int(x1 - mx)), max(0, int(y1 - my))
        x2, y2 = min(w, int(x2 + mx)), min(h, int(y2 + my))
        if x2 - x1 >= ROI_MIN_PX and y2 - y1 >= ROI_MIN_PX:
            out.append((x1, y1, x2 - x1, y2 - y1))
    out.sort(key=lambda r: r[2] * r[3], reverse=True)
    return out[:ROI_MAX]


class QualityProbe:
    """Compare mode: decode what each encoder produced and compare it with the picture it was given
    (luma PSNR), separately inside the target regions and in the background. Both outputs are
    measured over the same regions, whether ROI is on or not. Every encoded frame is decoded (P
    frames need their predecessors); PSNR is computed on every EVERY-th frame."""
    EVERY = 10
    WINDOW_S = 20.0

    def __init__(self):
        self.dec = {}
        self.hist = {}                  # name -> deque[(time, psnr_roi, psnr_bg)]
        self.n = 0

    def reset(self, name):
        self.dec.pop(name, None)

    def _decoder(self, name):
        d = self.dec.get(name)
        if d is None:
            d = av.CodecContext.create("h264", "r")
            d.thread_count = 1          # no frame threading: each frame comes out right away
            self.dec[name] = d
        return d

    def feed(self, img, rois, outputs):
        """outputs: {name: [(bytes, pts, keyframe)] written for this frame}."""
        self.n += 1
        src = mask = None
        if self.n % self.EVERY == 0 and rois:
            src = (img.y.astype(np.float32) if hasattr(img, "y")      # draw.Yuv420 (yuv_direct)
                   else luma(av.VideoFrame.from_ndarray(img, format="bgr24").reformat(format="yuv420p")))
            mask = np.zeros(src.shape, bool)
            for x, y, w, h in rois:
                mask[y:y + h, x:x + w] = True
            if mask.all() or not mask.any():
                src = None              # no background (or no target) to compare: skip this sample
        for name, packets in outputs.items():
            if not packets:
                continue
            dec, frames = self._decoder(name), []
            for data, pts, _ in packets:
                p = av.Packet(data)
                p.pts = pts
                try:
                    frames += dec.decode(p)
                except av.FFmpegError:  # joined mid-GOP: nothing decodes until a keyframe
                    pass
            if src is None or not frames:   # still decoded: later P frames need this one
                continue
            err = (luma(frames[-1]) - src) ** 2
            self.hist.setdefault(name, deque()).append((time.monotonic(), psnr(err[mask]), psnr(err[~mask])))

    def stats(self):
        now, out = time.monotonic(), {}
        for name, h in self.hist.items():
            while h and now - h[0][0] > self.WINDOW_S:
                h.popleft()
            if h:
                out[name] = {"psnr_roi": round(sum(x[1] for x in h) / len(h), 2),
                             "psnr_bg": round(sum(x[2] for x in h) / len(h), 2), "samples": len(h)}
        return out


def luma(frame):
    """Y plane of a yuv420p frame as float32 (without the row padding)."""
    p = frame.planes[0]
    return np.frombuffer(p, np.uint8).reshape(p.height, p.line_size)[:, :p.width].astype(np.float32)


def psnr(sq_err):
    mse = float(sq_err.mean()) if sq_err.size else 0.0
    return 99.0 if mse <= 1e-6 else 10 * np.log10(255 ** 2 / mse)


class VideoCtl:
    def __init__(self, ref_dst=None):
        self.ref_dst = ref_dst          # where the compare stream goes (None: compare unavailable)
        self.cfg = {}
        self.ref = None                 # baseline Encoder while compare is on
        self.probe = None               # QualityProbe while compare is on
        self._lock = threading.Lock()
        self._viewer = False
        self._last_target = None        # monotonic time targets were last seen
        self._alarm_seen = None
        self._ref_failed = 0.0

    # ---- from other threads ----
    def set_config(self, cfg):
        cfg = dict(cfg or {})
        if cfg != self.cfg:
            log.info("video features: %s", {k: v for k, v in cfg.items() if v} or "all off")
        self.cfg = cfg

    def viewer_joined(self):
        with self._lock:
            self._viewer = True

    def on(self, name):
        return bool(self.cfg.get(name))

    # ---- decode loop, once per frame ----
    def apply(self, enc, track_views, alarm_time, w, h):
        """Set up the main encoder for the next frame. Returns (rois for the encoder or None,
        the target regions for quality measurement)."""
        regions = roi_boxes(track_views, w, h) if (self.on("roi") or self.on("compare")) else []
        n_tracks = len(track_views)
        dyn = self.on("dyn_gop")
        enc.gop_s = GOP_LONG if dyn else GOP_FIXED
        now = time.monotonic()
        with self._lock:
            viewer, self._viewer = self._viewer, False
        if n_tracks:
            if dyn and self._last_target is not None and now - self._last_target >= QUIET_SEC:
                enc.request_keyframe("activity")
            self._last_target = now
        if alarm_time is not None and alarm_time != self._alarm_seen:
            self._alarm_seen = alarm_time
            if dyn:
                enc.request_keyframe("alarm")
        if dyn and viewer:
            enc.request_keyframe("viewer")
        return (regions if self.on("roi") else None), regions

    def encode_ref(self, img, t, w, h, fps):
        """Compare mode: the same frame through a baseline encoder (opened / closed on demand).
        Returns the packets it wrote, or None."""
        want = self.on("compare") and self.ref_dst
        if want and self.ref is None and time.monotonic() - self._ref_failed > REF_RETRY_SEC:
            try:
                self.ref = Encoder(self.ref_dst, w, h, fps, gop_s=GOP_FIXED)
                self.probe = self.probe or QualityProbe()
                self.probe.reset("ref")
                log.info("compare stream on: %s", self.ref_dst)
            except Exception as e:
                log.warning("compare stream failed: %s", e)
                self._ref_failed = time.monotonic()
        elif not want and self.ref is not None:
            self.close_ref()
            log.info("compare stream off")
        if self.ref is not None:
            try:
                return self.ref.encode(img, t)
            except Exception as e:
                log.warning("compare stream error: %s", e)
                self.close_ref()
                self._ref_failed = time.monotonic()
        return None

    def measure(self, img, regions, main_packets, ref_packets, enc):
        """Compare mode: quality of both outputs. The main decoder joins mid-stream, so it asks
        for one keyframe to start from."""
        if self.probe is None or self.ref is None:
            return
        if "main" not in self.probe.dec:
            enc.request_keyframe("compare")
        self.probe.feed(img, regions, {"main": main_packets, "ref": ref_packets})

    def close_ref(self):
        ref, self.ref, self.probe = self.ref, None, None
        if ref:
            ref.close()

    def stats(self):
        ref, probe = self.ref, self.probe
        return {"video": self.cfg, "ref": ref.stats() if ref else None,
                "quality": probe.stats() if probe and ref else None}
