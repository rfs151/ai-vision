"""INT8 post-training quantization (NNCF) of an exported OpenVINO YOLO model.

    .venv-export/bin/python scripts/quantize_int8.py            # yolo11n and yolo11n-pose
    .venv-export/bin/python scripts/quantize_int8.py yolo11n

Reads worker/models/<name>_openvino_model, writes worker/models/<name>_int8_openvino_model
(the worker picks it up for the int8 video feature). No retraining: calibration only collects
the value ranges of each layer, from frames of data/samples/*.mp4 (scripts/get_samples.sh)
letterboxed exactly like worker/detector.py, half square and half rectangular input. The model
stays dynamic-shape. The head's box/keypoint decoding (Add/Sub/Mul/Div, DFL) and Sigmoid stay
FP32, as in Ultralytics' own INT8 export: they lose the most accuracy when quantized.
Runs in .venv-export (needs nncf, see requirements-export.txt; opencv comes with ultralytics).
"""
import glob
import sys
from pathlib import Path

import cv2
import nncf
import numpy as np
import openvino as ov

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "worker"))
from detector import YoloDetector  # noqa: E402

MODELS = ROOT / "worker" / "models"
FRAMES_PER_VIDEO = 25
EVERY = 7                       # every 7th frame, so frames are not near-duplicates


def calibration_frames(det):
    videos = sorted(set(glob.glob(str(ROOT / "data/samples/*.mp4")) + glob.glob(str(ROOT / "data/samples/raw/*.mp4"))))
    if not videos:
        sys.exit("no videos in data/samples: run scripts/get_samples.sh first")
    blobs, k = [], 0
    for v in videos:
        cap, i, n = cv2.VideoCapture(v), 0, 0
        while n < FRAMES_PER_VIDEO:
            ok, img = cap.read()
            if not ok:
                break
            i += 1
            if (i - 1) % EVERY:
                continue
            hw = det.input_shape(*img.shape[:2], rect=(k % 2 == 1))
            lb = det._letterbox(img, hw)[0]
            blobs.append(lb[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0)
            k, n = k + 1, n + 1
        cap.release()
    print(f"calibration: {len(blobs)} frames from {len(videos)} videos")
    return blobs


def quantize(name):
    src = MODELS / f"{name}_openvino_model"
    out = MODELS / f"{name}_int8_openvino_model"
    xml = next(src.glob("*.xml"))
    det = YoloDetector(src)
    if not det.rect_ok:
        sys.exit(f"{src} is a fixed-shape export: re-run scripts/export_model.sh {name} first")
    model = ov.Core().read_model(str(xml))
    head = "model.23"           # Detect / Pose head of YOLO11n
    ignored = nncf.IgnoredScope(
        patterns=[f".*{head}/.*/Add", f".*{head}/.*/Sub*", f".*{head}/.*/Mul*", f".*{head}/.*/Div*",
                  f".*{head}\\.dfl.*"],
        types=["Sigmoid"], validate=False)
    blobs = calibration_frames(det)
    q = nncf.quantize(model, nncf.Dataset(blobs), preset=nncf.QuantizationPreset.MIXED,
                      subset_size=len(blobs), ignored_scope=ignored)
    out.mkdir(parents=True, exist_ok=True)
    ov.save_model(q, str(out / xml.name), compress_to_fp16=False)
    for f in src.glob("*.yaml"):
        (out / f.name).write_bytes(f.read_bytes())
    mb = lambda d: sum(p.stat().st_size for p in d.glob("*.bin")) / 1e6  # noqa: E731
    print(f"{out}: {mb(out):.1f} MB (FP32 {mb(src):.1f} MB)")


if __name__ == "__main__":
    for name in sys.argv[1:] or ["yolo11n", "yolo11n-pose"]:
        quantize(name)
