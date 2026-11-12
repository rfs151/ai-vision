"""YOLO detector running on OpenVINO (CPU by default).

Input: a BGR frame (numpy, HxWx3). Output: a list of Detection.
Pre/post-processing is done by hand (letterbox + NMS), so runtime only
needs openvino + numpy + opencv; no torch/ultralytics.

Supports YOLOv8 / YOLO11 detection models exported to OpenVINO or ONNX,
whose output is [1, 4 + num_classes, N] (cx, cy, w, h, class scores...), and pose models
(yolo11n-pose): [1, 4 + 1 + 17 * 3, N], one class (person) plus 17 COCO keypoints (x, y, conf).

Rectangular input (rect=True): the picture is scaled exactly as for the square input (long side to
imgsz), but only padded up to a multiple of 32 instead of to imgsz x imgsz. A 16:9 picture becomes
640x384 instead of 640x640 (about 40 % less compute), objects keep the same size, so detections
are the same. Needs a model exported with dynamic=True (scripts/export_model.sh): a fixed-shape
export bakes the 640x640 anchor grid into the graph and cannot be reshaped; such a model keeps
using the square input.
"""
import math
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import openvino as ov

COCO_NAMES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
    "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
    "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]


@dataclass
class Detection:
    x1: float
    y1: float
    x2: float
    y2: float
    score: float
    cls: int
    name: str
    kpts: np.ndarray = None   # pose models: [17, 3] x, y (pixels), confidence 0..1


class YoloDetector:
    def __init__(self, model_path, device="CPU", imgsz=640, conf=0.35, iou=0.5,
                 classes=None, names=COCO_NAMES, threads=2, nkpt=0):
        path = Path(model_path)
        if path.is_dir():  # Ultralytics OpenVINO export dir: *_openvino_model/xxx.xml
            path = next(path.glob("*.xml"))
        core = ov.Core()
        # LATENCY hint: one request at a time. Default thread count uses every core and
        # spin-waits: on i5-13500H yolo11n, auto = 43 ms @ 158% CPU vs 2 threads = 44 ms @ 43%.
        # With many streams per machine, few threads per stream is far more efficient.
        cfg = {"PERFORMANCE_HINT": "LATENCY"}
        if device == "CPU" and threads:
            cfg["INFERENCE_NUM_THREADS"] = threads
        if device == "CPU":
            # Thread pinning is decided per process: with several workers they all pin to the same
            # cores and queue behind each other. 6 workers: pinned 380 ms per frame, unpinned 62 ms.
            cfg["ENABLE_CPU_PINNING"] = False
        self.core, self.device, self.cfg = core, device, cfg
        self.ov_model = core.read_model(str(path))
        # Dynamic export: input [?, 3, ?, ?]; compiled once per input shape (cached)
        self.dynamic = self.ov_model.inputs[0].partial_shape.is_dynamic
        self.compiled = {}
        self.imgsz = imgsz
        self.input_hw = (imgsz, imgsz)  # shape of the last inference
        self.conf = conf
        self.iou = iou
        self.names = names
        self.nkpt = nkpt            # 17 for a pose model: its only class is person (id 0)
        # Keep only these class ids (None = all)
        self.classes = None if classes is None else set(classes)

    @property
    def rect_ok(self):
        """Whether this model can take a rectangular input (dynamic-shape export)."""
        return self.dynamic

    def input_shape(self, h, w, rect=False):
        """(height, width) of the model input for an h x w picture."""
        if not (rect and self.dynamic):
            return self.imgsz, self.imgsz
        r = self.imgsz / max(h, w)
        return (min(self.imgsz, math.ceil(round(h * r) / 32) * 32),
                min(self.imgsz, math.ceil(round(w * r) / 32) * 32))

    def _compiled(self, hw):
        m = self.compiled.get(hw)
        if m is None:
            if self.dynamic:
                self.ov_model.reshape([1, 3, hw[0], hw[1]])
            m = self.compiled[hw] = self.core.compile_model(self.ov_model, self.device, self.cfg)
        return m

    def prepare(self, h, w, rect=False):
        """Compile the input shape for an h x w picture now, so the first timed inference does
        not include the compile (a few hundred ms)."""
        self._compiled(self.input_shape(h, w, rect))

    def _letterbox(self, img, hw):
        """Resize keeping aspect ratio (long side to imgsz), pad to hw = (height, width) with gray."""
        h, w = img.shape[:2]
        r = self.imgsz / max(h, w)
        nh, nw = round(h * r), round(w * r)
        top = (hw[0] - nh) // 2
        left = (hw[1] - nw) // 2
        canvas = np.full((hw[0], hw[1], 3), 114, dtype=np.uint8)
        canvas[top:top + nh, left:left + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        return canvas, r, left, top

    def detect(self, frame, out_size=None, rect=False):
        """out_size: (w, h) of the original picture when `frame` is a scaled-down copy of it
        (yuv_direct); boxes and keypoints come back in original coordinates.
        rect: rectangular input (see module docstring); ignored by a fixed-shape model."""
        h, w = frame.shape[:2]
        hw = self.input_shape(h, w, rect)   # a scaled-down copy has the same aspect ratio
        self.input_hw = hw
        img, r, dx, dy = self._letterbox(frame, hw)
        if out_size:
            r *= w / out_size[0]        # dividing by r below then also scales back up
            w, h = out_size
        # BGR HWC uint8 -> RGB NCHW float32 [0,1]
        blob = img[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        out = self._compiled(hw)(blob)[0][0]  # [4 + nc, N]
        out = out.T                           # [N, 4 + nc (+ nkpt * 3)]
        scores_all = out[:, 4:out.shape[1] - self.nkpt * 3]
        cls = scores_all.argmax(1)
        scores = scores_all[np.arange(len(cls)), cls]

        keep = scores >= self.conf
        if self.classes is not None:
            keep &= np.isin(cls, list(self.classes))
        if not keep.any():
            return []
        boxes, scores, cls = out[keep, :4], scores[keep], cls[keep]
        kpts = None
        if self.nkpt:
            kpts = out[keep, out.shape[1] - self.nkpt * 3:].reshape(-1, self.nkpt, 3).copy()
            kpts[..., 0] = (kpts[..., 0] - dx) / r
            kpts[..., 1] = (kpts[..., 1] - dy) / r

        # cx,cy,w,h (letterboxed) -> x,y,w,h (original image)
        xywh = np.empty_like(boxes)
        xywh[:, 0] = (boxes[:, 0] - boxes[:, 2] / 2 - dx) / r
        xywh[:, 1] = (boxes[:, 1] - boxes[:, 3] / 2 - dy) / r
        xywh[:, 2] = boxes[:, 2] / r
        xywh[:, 3] = boxes[:, 3] / r

        # Per-class NMS: offset boxes by class id so different classes never suppress each other
        offset = cls[:, None].astype(np.float32) * 10000.0
        nms_boxes = np.concatenate([xywh[:, :2] + offset, xywh[:, 2:]], axis=1)
        idx = cv2.dnn.NMSBoxes(nms_boxes.tolist(), scores.tolist(), self.conf, self.iou)
        if len(idx) == 0:
            return []

        dets = []
        for i in np.array(idx).flatten():
            x, y, bw, bh = xywh[i]
            c = int(cls[i])
            dets.append(Detection(
                x1=float(max(0, x)), y1=float(max(0, y)),
                x2=float(min(w, x + bw)), y2=float(min(h, y + bh)),
                score=float(scores[i]), cls=c,
                name=self.names[c] if c < len(self.names) else str(c),
                kpts=None if kpts is None else kpts[i],
            ))
        return dets


def box_iou(a, b):
    x1, y1, x2, y2 = max(a.x1, b.x1), max(a.y1, b.y1), min(a.x2, b.x2), min(a.y2, b.y2)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a.x2 - a.x1) * (a.y2 - a.y1) + (b.x2 - b.x1) * (b.y2 - b.y1) - inter
    return inter / union if union > 0 else 0.0


def compare_dets(base, other, conf=0.35, iou=0.5):
    """Baseline boxes (score >= conf) found again by `other` (rect input, INT8 model): same class,
    IoU >= iou. Returns (n_base, n_found, score changes of the found ones). A box whose new score
    fell below conf counts as found here; the score change shows it."""
    ref = [d for d in base if d.score >= conf]
    used, deltas = set(), []
    for a in ref:
        best, bi = 0.0, None
        for j, b in enumerate(other):
            if j in used or b.cls != a.cls:
                continue
            v = box_iou(a, b)
            if v > best:
                best, bi = v, j
        if bi is not None and best >= iou:
            used.add(bi)
            deltas.append(other[bi].score - a.score)
    return len(ref), len(deltas), deltas


class InferProbe:
    """Compare mode with rect_input and/or int8 on: every EVERY-th inference, run the baseline
    (FP32 model, square input) on the same picture as well, and keep the last WINDOW_S of
    (baseline ms, current ms, boxes, found, score change)."""
    EVERY = 10
    WINDOW_S = 20.0

    def __init__(self):
        self.n = 0
        self.hist = deque()

    def reset(self):
        self.n = 0
        self.hist.clear()

    def due(self):
        self.n += 1
        return self.n % self.EVERY == 0

    def run(self, base, img, dets, ms, out_size=None):
        """base: the FP32 detector (run with the square input); dets, ms: the current result."""
        h, w = img.shape[:2]
        base.prepare(h, w, rect=False)
        hw = base.input_hw
        t = time.perf_counter()
        ref = base.detect(img, out_size=out_size, rect=False)
        base_ms = (time.perf_counter() - t) * 1000
        base.input_hw = hw                  # stats show the input actually in use
        n, found, deltas = compare_dets(ref, dets)
        now = time.monotonic()
        self.hist.append((now, base_ms, ms, n, found, sum(deltas), len(deltas)))
        while self.hist and self.hist[0][0] < now - self.WINDOW_S:
            self.hist.popleft()

    def stats(self):
        hist = tuple(self.hist)         # snapshot: called from another thread
        if not hist:
            return {}
        k = len(hist)
        n = sum(x[3] for x in hist)
        found = sum(x[4] for x in hist)
        nd = sum(x[6] for x in hist)
        return {
            "probe_n": k,
            "probe_base_ms": round(sum(x[1] for x in hist) / k, 1),
            "probe_ms": round(sum(x[2] for x in hist) / k, 1),
            "probe_boxes": n,
            "probe_found_pct": round(found / n * 100, 1) if n else None,
            "probe_score_delta": round(sum(x[5] for x in hist) / nd, 3) if nd else None,
        }
