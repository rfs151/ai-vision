"""rect_input: pad the picture only to a multiple of 32 instead of to 640x640."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
from detector import Detection, InferProbe, YoloDetector, compare_dets  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "worker" / "models" / "yolo11n_openvino_model"
SAMPLE = ROOT / "data" / "samples" / "people.mp4"


def bare(dynamic=True):
    """A detector without a model, for the shape arithmetic."""
    d = object.__new__(YoloDetector)
    d.imgsz, d.dynamic = 640, dynamic
    return d


@pytest.mark.parametrize("w,h,shape", [
    (960, 540, (384, 640)),     # 16:9 -> 640x360, padded to 384
    (1920, 1080, (384, 640)),
    (768, 576, (480, 640)),     # 4:3 -> 640x480, a multiple of 32: no padding
    (640, 240, (256, 640)),
    (540, 960, (640, 384)),     # portrait
    (640, 640, (640, 640)),
])
def test_input_shape(w, h, shape):
    assert bare().input_shape(h, w, rect=True) == shape
    assert bare().input_shape(h, w, rect=False) == (640, 640)


def test_fixed_shape_model_keeps_square_input():
    assert bare(dynamic=False).input_shape(540, 960, rect=True) == (640, 640)


def det(x1, y1, x2, y2, score, cls=0):
    return Detection(x1, y1, x2, y2, score, cls, "person")


def test_compare_dets():
    square = [det(0, 0, 100, 100, 0.9), det(200, 200, 300, 300, 0.5), det(400, 0, 450, 50, 0.2)]
    rect = [det(2, 2, 100, 100, 0.85), det(200, 200, 300, 300, 0.3, cls=2), det(600, 0, 700, 50, 0.9)]
    n, found, deltas = compare_dets(square, rect)
    # 0.2 box is below conf: not counted; the second one changed class: not found
    assert (n, found) == (2, 1) and deltas == [pytest.approx(-0.05)]


def test_probe_window_stats():
    p = InferProbe()
    assert [p.due() for _ in range(20)].count(True) == 2
    assert p.stats() == {}


def frames(n=8):
    av = pytest.importorskip("av")
    out = []
    with av.open(str(SAMPLE)) as c:
        for i, f in enumerate(c.decode(video=0)):
            if i % 10 == 0:
                out.append(f.to_ndarray(format="bgr24"))
            if len(out) == n:
                break
    return out


def busiest(detector):
    """The sample frame with the most confident boxes (the first frames are nearly empty)."""
    return max(frames(), key=lambda f: sum(d.score >= 0.35 for d in detector.detect(f)))


@pytest.fixture(scope="module")
def detector():
    if not MODEL.exists() or not SAMPLE.exists():
        pytest.skip("model or sample video missing")
    d = YoloDetector(MODEL, conf=0.1)
    if not d.rect_ok:
        pytest.skip("fixed-shape model export (re-run scripts/export_model.sh)")
    return d


def test_rect_finds_the_same_boxes(detector):
    """Same objects at the same scale: the rect input finds the square input's boxes again, in
    original-picture coordinates."""
    total = found = 0
    for img in frames():
        square = detector.detect(img)
        rect = detector.detect(img, rect=True)
        assert detector.input_hw == (384, 640)
        n, f, deltas = compare_dets(square, rect)
        total, found = total + n, found + f
        h, w = img.shape[:2]
        assert all(0 <= d.x1 < d.x2 <= w and 0 <= d.y1 < d.y2 <= h for d in rect)
    assert total and found / total >= 0.95


def test_rect_with_scaled_copy(detector):
    """yuv_direct hands over a scaled-down copy plus the full size: boxes come back in full size."""
    import cv2
    img = busiest(detector)
    h, w = img.shape[:2]
    big = cv2.resize(img, (w * 2, h * 2))
    full = detector.detect(big, rect=True)
    small = detector.detect(img, out_size=(w * 2, h * 2), rect=True)
    n, found, _ = compare_dets(full, small)
    assert n and found == n


def test_probe_runs_square_on_the_same_frame(detector):
    img = busiest(detector)
    rect = detector.detect(img, rect=True)
    p = InferProbe()
    p.run(detector, img, rect, ms=10.0)
    s = p.stats()
    assert detector.input_hw == (384, 640)          # probe leaves the rect shape in the stats
    assert s["probe_n"] == 1 and s["probe_ms"] == 10.0 and s["probe_base_ms"] > 0
    assert s["probe_found_pct"] >= 90
    assert np.isfinite(s["probe_score_delta"])


INT8 = ROOT / "worker" / "models" / "yolo11n_int8_openvino_model"


def test_int8_finds_the_fp32_boxes(detector):
    """INT8 model (scripts/quantize_int8.py) against the FP32 baseline on the same frames."""
    if not INT8.exists():
        pytest.skip("no INT8 model (run scripts/quantize_int8.py)")
    q = YoloDetector(INT8, conf=0.1)
    assert q.rect_ok                                 # stays dynamic-shape
    total = found = 0
    deltas = []
    for img in frames():
        for rect in (False, True):
            n, f, d = compare_dets(detector.detect(img), q.detect(img, rect=rect))
            total, found, deltas = total + n, found + f, deltas + d
    assert total and found / total >= 0.95
    assert abs(sum(deltas) / len(deltas)) < 0.05
