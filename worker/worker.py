"""AI worker: pull a stream from ZLMediaKit, detect + track, evaluate rules, push the result back.

    python worker/worker.py --src rtsp://127.0.0.1/live/test --dst rtsp://127.0.0.1/ai/test \
        --rules rules/sample.json

Pipeline:
    PyAV decode (main thread, every frame, time t = frame pts)
      ├─> latest frame slot ──> analysis thread (every 1/det_fps s):
      │       YOLO (OpenVINO) -> ByteTrack -> rules -> alarms (snapshot + clip job)
      └─> draw tracks extrapolated to t + zones/lines ──> x264 (in-process, PyAV) ─┬─> RTSP push to ZLM
                                                                                   └─> 2 s segments in /dev/shm
                                                    (clip.py joins them into ±10 s alarm clips)
Analysis runs on the newest frame only, so slow inference never piles up latency.
"""
import argparse
import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import av

import draw
from alarm import AlarmSink
from apiclient import ApiClient
from clip import ClipRecorder
from detector import COCO_NAMES, YoloDetector
from encoder import Encoder
from rules import RuleEngine
from tracker import ByteTracker, views
from video import VideoCtl

log = logging.getLogger("worker")
ROOT = Path(__file__).resolve().parent.parent
STOP = threading.Event()          # set by SIGTERM
BANNER_SEC = 3.0


class AnalysisThread(threading.Thread):
    """Detect -> track -> rules on the latest frame, at a fixed rate."""

    def __init__(self, detector, tracker, rules_cfg, sink, det_fps, clips=None):
        super().__init__(daemon=True)
        self.clips = clips              # ClipRecorder or None
        self.detector = detector
        self.tracker = tracker
        self.rules_cfg = rules_cfg
        self.engine = None              # created on first frame (needs frame size)
        self.sink = sink
        self.interval = 1.0 / det_fps
        self.cond = threading.Condition()
        self.pending = None             # (frame, t, wall time the frame arrived)
        self.on_tracks = None           # callback(payload) for browser overlays (ApiClient.send_tracks)
        self.stream = ""
        self.running = True
        self._reset = False
        self._new_rules = None          # set by the API poller thread, applied in run()
        self.out_fps = 0.0
        self.resolution = None
        self.connected = False
        self.encoder = None             # Encoder of the current connection (None with --encoder ffmpeg)
        self.video = VideoCtl()         # video features (dynamic keyframes, compare stream)
        self._cpu = (time.monotonic(), time.process_time())
        # Published state, read by the drawing thread (replaced atomically, never mutated)
        self.track_views = []
        self.infer_ms = 0.0
        self.alarm = None               # (wall_time, text, track_ids, zone_ids)

    def submit(self, frame, t):
        with self.cond:
            self.pending = (frame, t, time.time())
            self.cond.notify()

    def reset(self):
        """Stream reconnected: timestamps restart, so drop all tracking/rule state."""
        self._reset = True

    def set_rules(self, cfg):
        """Hot reload (called from another thread). Engine is rebuilt on the next frame."""
        self._new_rules = cfg

    def stats(self):
        e = self.engine
        # Whole worker process (decode, inference, encode, compare encode), % of one CPU
        wall, cpu = time.monotonic(), time.process_time()
        cpu_pct = round((cpu - self._cpu[1]) / max(1e-3, wall - self._cpu[0]) * 100, 1)
        self._cpu = (wall, cpu)
        return {
            "connected": self.connected, "resolution": self.resolution,
            "out_fps": round(self.out_fps, 1), "infer_ms": round(self.infer_ms, 1),
            "tracks": len(self.track_views), "cpu": cpu_pct,
            **(self.encoder.stats() if self.encoder else {}),
            **self.video.stats(),
            "zones": {z.id: z.count for z in e.zones} if e else {},
            "lines": {l.id: {"in": l.n_in, "out": l.n_out} for l in e.lines} if e else {},
        }

    def run(self):
        while self.running:
            t0 = time.monotonic()
            with self.cond:
                while self.pending is None and self.running:
                    self.cond.wait(0.5)
                item, self.pending = self.pending, None
            if item is None:
                continue
            frame, t, arrived = item
            if self._new_rules is not None:
                self.rules_cfg, self._new_rules = self._new_rules, None
                self.engine = None      # rebuilt below with the new config (counters restart)
            if self._reset:
                self._reset = False
                self.tracker.reset()
                if self.engine:
                    self.engine.reset()
                self.track_views = []
            try:
                self._process(frame, t)
                if self.on_tracks:
                    self.on_tracks(self._tracks_payload(frame.shape, arrived))
            except Exception:
                log.exception("analysis failed")
            sleep = self.interval - (time.monotonic() - t0)
            if sleep > 0:
                time.sleep(sleep)

    def _tracks_payload(self, shape, arrived):
        """Boxes normalized to 0..1 plus Kalman velocity (per second), so a browser can draw them
        over the raw stream at any size and move them smoothly between detections.
        tr: [id, class, score, x1, y1, x2, y2, vx, vy, in_alarm]"""
        h, w = shape[:2]
        alarm = self.alarm
        hot = alarm[2] if alarm and time.monotonic() - alarm[0] < BANNER_SEC else ()
        tracks = []
        for v in self.track_views:
            x1, y1, x2, y2 = v.box
            vx, vy = v.kx[4], v.kx[5]
            tracks.append([v.id, v.name, round(float(v.score), 2),
                           round(x1 / w, 4), round(y1 / h, 4), round(x2 / w, 4), round(y2 / h, 4),
                           round(float(vx) / w, 4), round(float(vy) / h, 4), int(v.id in hot)])
        # p: seconds from frame arrival to now (decode wait + inference). Measured on this machine,
        # so the browser can use it without comparing clocks: that frame is on screen about
        # (receive time - p + the player's own buffering delay).
        return {"s": self.stream, "a": round(arrived, 3), "p": round(time.time() - arrived, 3),
                "w": w, "h": h, "tr": tracks}

    def _process(self, frame, t):
        if self.engine is None and self.rules_cfg:
            h, w = frame.shape[:2]
            self.engine = RuleEngine(self.rules_cfg, w, h)

        t1 = time.perf_counter()
        dets = self.detector.detect(frame)
        self.infer_ms = (time.perf_counter() - t1) * 1000
        tracks = self.tracker.update(dets, t)
        events = self.engine.update(tracks, t, self.tracker.removed) if self.engine else []
        self.track_views = views(tracks)

        alarms = [e for e in events if e.alarm]
        for e in events:
            if not e.alarm:
                self.sink.emit(e, None)
        if alarms:
            # One snapshot per event, drawn on the exact frame that triggered it
            ids = {e.track_id for e in alarms}
            zones = {e.rule_id for e in alarms}
            snap = frame.copy()
            draw.draw_rules(snap, self.engine, zones)
            draw.draw_tracks(snap, tracks, alarm_track_ids=ids)
            text = " | ".join(f"{e.type} @ {e.rule_name}" + (f" #{e.track_id}" if e.track_id else "")
                              for e in alarms)
            draw.draw_banner(snap, "ALARM: " + text)
            job = self.clips.request() if self.clips else None   # alarms of one frame share a clip
            for e in alarms:
                if job:
                    job.expect()
                self.sink.emit(e, snap, job)
            self.alarm = (time.monotonic(), "ALARM: " + text, ids, zones)


class FfmpegEncoder:
    """The previous encoder: an ffmpeg subprocess fed raw frames through a pipe (--encoder ffmpeg).
    Kept for before/after comparisons; no per-frame control and no encoder stats."""

    def __init__(self, dst, w, h, fps, clips=None):
        self.proc = start_encoder(dst, w, h, fps, clips.tee_output() if clips else None)

    def encode(self, img, t):
        try:
            self.proc.stdin.write(img.tobytes())
        except BrokenPipeError:
            raise RuntimeError(f"encoder exited (code {self.proc.poll()})") from None

    def close(self):
        try:
            self.proc.stdin.close()
        except BrokenPipeError:
            pass
        self.proc.wait(timeout=5)


def start_encoder(dst, w, h, fps, extra_out=None):
    """ffmpeg reads raw BGR frames from stdin, encodes H.264 low-latency, pushes RTSP.
    extra_out: a second ffmpeg tee output (alarm clip segments), same encoded stream, no extra cost."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", f"{fps}", "-i", "-",
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-g", str(int(fps * 2)), "-bf", "0",
        # Cap keyframe size (~30 KB instead of ~135 KB). WebRTC sends a frame as one burst of UDP
        # packets; WSL -> Windows drops part of bursts over ~90 packets and the player froze.
        "-crf", "23", "-maxrate", "1200k", "-bufsize", "300k",
        # SPS/PPS before every keyframe (x264 writes them only once for RTSP output). Without them
        # a WebRTC player whose decoder restarts mid-stream (Windows Chrome) never decodes again.
        "-bsf:v", "dump_extra=freq=keyframe",
    ]
    # 1200-byte RTP: ZLM forwards RTSP-pushed RTP to WebRTC players unchanged. ffmpeg's default
    # (1472) + WebRTC header extensions + SRTP exceeds a 1500 MTU; on such paths (e.g. WSL
    # mirrored networking) every large packet is dropped and WebRTC never decodes a frame.
    if extra_out:
        cmd += ["-map", "0:v", "-f", "tee", f"[f=rtsp:rtsp_transport=tcp:pkt_size=1200]{dst}|{extra_out}"]
    else:
        cmd += ["-f", "rtsp", "-rtsp_transport", "tcp", "-pkt_size", "1200", dst]
    log.info("encoder: %s", " ".join(cmd))
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def render(img, t, an, out_fps):
    alarm = an.alarm
    active = alarm is not None and time.monotonic() - alarm[0] < BANNER_SEC
    draw.draw_rules(img, an.engine, alarm[3] if active else ())
    draw.draw_tracks(img, an.track_views, t, alarm[2] if active else ())
    draw.draw_status(img, f"out {out_fps:.1f}fps | infer {an.infer_ms:.0f}ms | tracks {len(an.track_views)}")
    if active:
        draw.draw_banner(img, alarm[1])


def run_once(args, an):
    """Process one connection to the source. Returns when the stream ends or errors."""
    log.info("opening %s", args.src)
    container = av.open(args.src, options={"rtsp_transport": "tcp", "timeout": "5000000"}, timeout=10)
    an.reset()
    vstream = container.streams.video[0]
    # SLICE, not AUTO: frame threading decodes up to <cpu count> frames in parallel, so each frame
    # comes out ~16 frames late (measured 1.39 s at 10 fps, vs 1.5 ms with slice threading). That
    # delayed the AI stream, the alarms and the browser overlay boxes by the same 1.4 s.
    vstream.thread_type = "SLICE"
    fps = float(vstream.average_rate or vstream.guessed_rate or 25)
    fps = fps if 1 <= fps <= 60 else 25
    encoder = None
    frames, t_start, out_fps = 0, time.monotonic(), 0.0
    wall0 = time.monotonic()
    try:
        for frame in container.decode(vstream):
            if STOP.is_set():
                return
            img = frame.to_ndarray(format="bgr24")
            t = frame.time if frame.time is not None else time.monotonic() - wall0
            if encoder is None:
                h, w = img.shape[:2]
                log.info("source %dx%d @ %.2f fps, encoder %s", w, h, fps, args.encoder)
                if args.encoder == "ffmpeg":
                    encoder = FfmpegEncoder(args.dst, w, h, fps, an.clips)
                else:
                    encoder = Encoder(args.dst, w, h, fps, an.clips.segment_pattern() if an.clips else None,
                                      an.clips.seg_sec if an.clips else 2)
                    an.encoder = encoder
                an.connected, an.resolution = True, f"{w}x{h}"
            h, w = img.shape[:2]
            an.submit(img.copy(), t)
            frames += 1
            elapsed = time.monotonic() - t_start
            if elapsed >= 1:
                out_fps, frames, t_start = frames / elapsed, 0, time.monotonic()
                an.out_fps = out_fps
            render(img, t, an, out_fps)
            if an.encoder:              # video features need the in-process encoder
                alarm = an.alarm
                rois, regions = an.video.apply(encoder, an.track_views, alarm[0] if alarm else None, w, h)
                ref_packets = an.video.encode_ref(img, t, w, h, fps)
                packets = encoder.encode(img, t, rois)
                an.video.measure(img, regions, packets, ref_packets, encoder)
            else:
                encoder.encode(img, t)
    finally:
        an.connected, an.out_fps, an.encoder = False, 0.0, None
        container.close()
        an.video.close_ref()
        if encoder:
            encoder.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="input stream, e.g. rtsp://127.0.0.1/live/test")
    ap.add_argument("--dst", required=True, help="output stream, e.g. rtsp://127.0.0.1/ai/test")
    ap.add_argument("--name", help="stream name for alarm output (default: last path part of --src)")
    ap.add_argument("--model", default=str(ROOT / "worker" / "models" / "yolo11n_openvino_model"))
    ap.add_argument("--device", default="CPU")
    ap.add_argument("--threads", type=int, default=2, help="OpenVINO CPU threads per worker, 0 = auto")
    ap.add_argument("--det-fps", type=float, default=5)
    ap.add_argument("--conf", type=float, default=0.4, help="high score threshold for tracking")
    ap.add_argument("--classes", default="person,bicycle,car,motorcycle,bus,truck",
                    help="comma separated class names, empty = all")
    ap.add_argument("--rules", help="rules JSON file (zones / lines); ignored with --api")
    ap.add_argument("--api", help="control service URL: rules from API (hot reload), alarms + heartbeat to API")
    ap.add_argument("--out", default=str(ROOT / "data" / "alarms"), help="alarm output dir (without --api)")
    ap.add_argument("--clip-pre", type=float, default=10, help="alarm clip: seconds before the alarm")
    ap.add_argument("--clip-post", type=float, default=10, help="alarm clip: seconds after the alarm")
    ap.add_argument("--encoder", choices=("pyav", "ffmpeg"), default="pyav",
                    help="pyav: x264 in this process (per-frame control, encoder stats); "
                         "ffmpeg: the previous ffmpeg subprocess, for comparison")
    ap.add_argument("--ref-dst", help="compare mode output (baseline encode), e.g. rtsp://127.0.0.1/ai_ref/test")
    ap.add_argument("--video", default="{}", help='video features as JSON without --api, e.g. {"dyn_gop": true}')
    ap.add_argument("--no-clips", action="store_true", help="no alarm clips (no segment ring)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # one line per heartbeat is noise
    # Manager stops us with SIGTERM: only set a flag. The decode loop sees it on the next frame
    # and leaves through its finally block (encoder closed after a whole frame, not mid-write).
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())

    classes = [COCO_NAMES.index(n.strip()) for n in args.classes.split(",")] if args.classes else None
    # Detector keeps low-score boxes too: ByteTrack uses them to keep occluded targets alive
    detector = YoloDetector(args.model, device=args.device, conf=0.1, classes=classes, threads=args.threads)
    tracker = ByteTracker(high_thresh=args.conf, new_thresh=args.conf)
    name = args.name or args.src.rstrip("/").split("/")[-1]

    client = None
    if args.api:
        client = ApiClient(args.api, name)
        rules_cfg, sink = client.wait_rules(), client
    else:
        rules_cfg = json.loads(Path(args.rules).read_text(encoding="utf-8")) if args.rules else None
        sink = AlarmSink(name, args.out)
    clips = None
    if not args.no_clips:
        # Segment ring in RAM (/dev/shm): ~150 KB/s per stream would otherwise hit the disk all the time
        base = Path("/dev/shm") if Path("/dev/shm").is_dir() else ROOT / "data"
        clips = ClipRecorder(base / "ai-vision-ring" / name, sink.save_clip, args.clip_pre, args.clip_post)
        clips.start()
    an = AnalysisThread(detector, tracker, rules_cfg, sink, args.det_fps, clips)
    an.stream = name
    an.video.ref_dst = args.ref_dst
    an.video.set_config(client.video if client else json.loads(args.video))
    if client:
        an.on_tracks = client.send_tracks
    an.start()
    if client:
        client.stats_fn = an.stats
        client.on_video = an.video.set_config
        client.on_viewer = an.video.viewer_joined
        client.start(an.set_rules)

    # Reconnect with exponential backoff: 1, 2, 4 ... 30 s
    backoff = 1
    while not STOP.is_set():
        t0 = time.monotonic()
        try:
            run_once(args, an)
        except KeyboardInterrupt:
            break
        except Exception as e:  # stream not found, network error, decode error...
            log.warning("stream error: %s", e)
        if STOP.is_set():
            break
        if time.monotonic() - t0 > 30:
            backoff = 1  # ran fine for a while, reset
        log.info("reconnect in %ds", backoff)
        STOP.wait(backoff)
        backoff = min(backoff * 2, 30)

    log.info("exiting")
    logging.shutdown()
    # Skip interpreter teardown: destroying OpenVINO's C++ worker threads there aborts
    # ("terminate called without an active exception") and dumps core, which made stops take ~5 s.
    os._exit(0)


if __name__ == "__main__":
    main()
