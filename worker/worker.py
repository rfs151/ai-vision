"""AI worker: pull a stream from ZLMediaKit, detect, draw boxes, push the result back.

    python worker/worker.py --src rtsp://127.0.0.1/live/test --dst rtsp://127.0.0.1/ai/test

Pipeline:
    PyAV decode (main thread, every frame)
      ├─> latest frame slot ──> detect thread (every 1/det_fps s, OpenVINO YOLO)
      └─> draw latest detections ──> ffmpeg (libx264) ──> RTSP push to ZLM
Detection runs on the newest frame only, so slow inference never piles up latency.
"""
import argparse
import logging
import subprocess
import threading
import time
from pathlib import Path

import av
import cv2

from detector import COCO_NAMES, YoloDetector

log = logging.getLogger("worker")

# Fixed color per class id (BGR)
PALETTE = [(56, 56, 255), (151, 157, 255), (31, 112, 255), (29, 178, 255), (49, 210, 207),
           (10, 249, 72), (23, 204, 146), (134, 219, 61), (52, 147, 26), (187, 212, 0)]


class DetectThread(threading.Thread):
    """Runs detection on the latest frame at a fixed rate."""

    def __init__(self, detector, det_fps):
        super().__init__(daemon=True)
        self.detector = detector
        self.interval = 1.0 / det_fps
        self.cond = threading.Condition()
        self.frame = None
        self.dets = []
        self.infer_ms = 0.0
        self.det_count = 0
        self.running = True

    def submit(self, frame):
        with self.cond:
            self.frame = frame
            self.cond.notify()

    def run(self):
        while self.running:
            t0 = time.monotonic()
            with self.cond:
                while self.frame is None and self.running:
                    self.cond.wait(0.5)
                frame, self.frame = self.frame, None
            if frame is None:
                continue
            t1 = time.perf_counter()
            dets = self.detector.detect(frame)
            self.infer_ms = (time.perf_counter() - t1) * 1000
            self.dets = dets  # atomic reference swap
            self.det_count += 1
            sleep = self.interval - (time.monotonic() - t0)
            if sleep > 0:
                time.sleep(sleep)


def draw(frame, dets, info):
    for d in dets:
        color = PALETTE[d.cls % len(PALETTE)]
        p1, p2 = (int(d.x1), int(d.y1)), (int(d.x2), int(d.y2))
        cv2.rectangle(frame, p1, p2, color, 2)
        label = f"{d.name} {d.score:.2f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(frame, (p1[0], p1[1] - th - 6), (p1[0] + tw + 4, p1[1]), color, -1)
        cv2.putText(frame, label, (p1[0] + 2, p1[1] - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(frame, info, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)
    return frame


def start_encoder(dst, w, h, fps):
    """ffmpeg reads raw BGR frames from stdin, encodes H.264 low-latency, pushes RTSP."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", f"{fps}", "-i", "-",
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-g", str(int(fps * 2)), "-bf", "0",
        "-f", "rtsp", "-rtsp_transport", "tcp", dst,
    ]
    log.info("encoder: %s", " ".join(cmd))
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def run_once(args, det_thread):
    """Process one connection to the source. Returns when the stream ends or errors."""
    log.info("opening %s", args.src)
    container = av.open(args.src, options={"rtsp_transport": "tcp", "timeout": "5000000"}, timeout=10)
    vstream = container.streams.video[0]
    vstream.thread_type = "AUTO"
    fps = float(vstream.average_rate or vstream.guessed_rate or 25)
    fps = fps if 1 <= fps <= 60 else 25
    encoder = None
    frames, t_start = 0, time.monotonic()
    out_fps = 0.0
    try:
        for frame in container.decode(vstream):
            img = frame.to_ndarray(format="bgr24")
            if encoder is None:
                h, w = img.shape[:2]
                log.info("source %dx%d @ %.2f fps", w, h, fps)
                encoder = start_encoder(args.dst, w, h, fps)
            det_thread.submit(img.copy())
            frames += 1
            elapsed = time.monotonic() - t_start
            if elapsed >= 1:
                out_fps, frames, t_start = frames / elapsed, 0, time.monotonic()
            info = f"out {out_fps:.1f}fps | infer {det_thread.infer_ms:.0f}ms | objs {len(det_thread.dets)}"
            draw(img, det_thread.dets, info)
            try:
                encoder.stdin.write(img.tobytes())
            except BrokenPipeError:
                log.error("encoder exited (code %s)", encoder.poll())
                return
    finally:
        container.close()
        if encoder:
            try:
                encoder.stdin.close()
            except BrokenPipeError:
                pass
            encoder.wait(timeout=5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="input stream, e.g. rtsp://127.0.0.1/live/test")
    ap.add_argument("--dst", required=True, help="output stream, e.g. rtsp://127.0.0.1/ai/test")
    ap.add_argument("--model", default=str(Path(__file__).parent / "models" / "yolo11n_openvino_model"))
    ap.add_argument("--device", default="CPU")
    ap.add_argument("--det-fps", type=float, default=5)
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--classes", default="person,bicycle,car,motorcycle,bus,truck",
                    help="comma separated class names, empty = all")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    classes = None
    if args.classes:
        classes = [COCO_NAMES.index(n.strip()) for n in args.classes.split(",")]
    detector = YoloDetector(args.model, device=args.device, conf=args.conf, classes=classes)
    det_thread = DetectThread(detector, args.det_fps)
    det_thread.start()

    # Reconnect with exponential backoff: 1, 2, 4 ... 30 s
    backoff = 1
    while True:
        t0 = time.monotonic()
        try:
            run_once(args, det_thread)
        except KeyboardInterrupt:
            break
        except Exception as e:  # stream not found, network error, decode error...
            log.warning("stream error: %s", e)
        if time.monotonic() - t0 > 30:
            backoff = 1  # ran fine for a while, reset
        log.info("reconnect in %ds", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()
