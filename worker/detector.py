"""YOLO detector running on OpenVINO (CPU by default).

Input: a BGR frame (numpy, HxWx3). Output: a list of Detection.
Pre/post-processing is done by hand (letterbox + NMS), so runtime only
needs openvino + numpy + opencv; no torch/ultralytics.

Supports YOLOv8 / YOLO11 detection models exported to OpenVINO or ONNX,
whose output is [1, 4 + num_classes, N] (cx, cy, w, h, class scores...), and pose models
(yolo11n-pose): [1, 4 + 1 + 17 * 3, N], one class (person) plus 17 COCO keypoints (x, y, conf).
"""
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
        self.model = core.compile_model(str(path), device, cfg)
        self.imgsz = imgsz
        self.conf = conf
        self.iou = iou
        self.names = names
        self.nkpt = nkpt            # 17 for a pose model: its only class is person (id 0)
        # Keep only these class ids (None = all)
        self.classes = None if classes is None else set(classes)

    def _letterbox(self, img):
        """Resize keeping aspect ratio, pad to imgsz x imgsz with gray."""
        h, w = img.shape[:2]
        r = min(self.imgsz / h, self.imgsz / w)
        nh, nw = round(h * r), round(w * r)
        top = (self.imgsz - nh) // 2
        left = (self.imgsz - nw) // 2
        canvas = np.full((self.imgsz, self.imgsz, 3), 114, dtype=np.uint8)
        canvas[top:top + nh, left:left + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        return canvas, r, left, top

    def detect(self, frame, out_size=None):
        """out_size: (w, h) of the original picture when `frame` is a scaled-down copy of it
        (yuv_direct); boxes and keypoints come back in original coordinates."""
        img, r, dx, dy = self._letterbox(frame)
        h, w = frame.shape[:2]
        if out_size:
            r *= w / out_size[0]        # dividing by r below then also scales back up
            w, h = out_size
        # BGR HWC uint8 -> RGB NCHW float32 [0,1]
        blob = img[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        out = self.model(blob)[0][0]          # [4 + nc, N]
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
