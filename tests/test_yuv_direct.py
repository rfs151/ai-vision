"""yuv_direct: drawing on yuv420p planes, encoding them as is, and detecting on a scaled copy."""
import sys
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import draw  # noqa: E402
from encoder import Encoder  # noqa: E402
from worker import SourceFrame  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
W, H = 320, 240


def base_bgr():
    """Smooth gradient picture (survives 4:2:0 subsampling, unlike noise)."""
    x = np.linspace(0, 255, W, dtype=np.float32)
    y = np.linspace(0, 255, H, dtype=np.float32)[:, None]
    return np.dstack([np.broadcast_to(x, (H, W)), np.broadcast_to(y, (H, W)),
                      np.full((H, W), 90, np.float32)]).astype(np.uint8)


def to_yuv(bgr):
    return draw.Yuv420(av.VideoFrame.from_ndarray(bgr, format="bgr24").reformat(format="yuv420p")
                       .to_ndarray(), W, H)


def to_bgr(yuv):
    return yuv.to_frame().to_ndarray(format="bgr24")


def psnr(a, b):
    mse = ((a.astype(np.float64) - b) ** 2).mean()
    return 99.0 if mse == 0 else 10 * np.log10(255 ** 2 / mse)


def track(tid, box, name="person"):
    return SimpleNamespace(id=tid, name=name, score=0.9, box=box, kpts=None, pose="")


def test_drawing_on_yuv_matches_drawing_on_bgr():
    """Same scene drawn on BGR (then converted, today's pipeline) and on the YUV planes directly:
    the pictures that reach the encoder look the same."""
    bgr = base_bgr()
    yuv = to_yuv(bgr)
    engine = SimpleNamespace(
        zones=[SimpleNamespace(id="z", name="zone", count=2, poly=np.array([[20, 120], [150, 120], [150, 220], [20, 220]], float))],
        lines=[SimpleNamespace(name="gate", p1=(180, 40), p2=(300, 200), n_in=1, n_out=0)])
    tracks = [track(1, (40, 30, 120, 110)), track(2, (200, 60, 280, 180), "car")]
    for img in (bgr, yuv):
        draw.draw_rules(img, engine)
        draw.draw_tracks(img, tracks, alarm_track_ids={2})
        draw.draw_status(img, "out 15.0fps | infer 40ms")
        draw.draw_banner(img, "ALARM: test")
    ref = to_yuv(bgr)                           # today: draw on BGR, then BGR -> yuv420p
    assert psnr(ref.y, yuv.y) > 45              # brightness: practically identical
    # Color: thin (2 px) lines land on the half-size chroma grid a little differently (more
    # saturated than BGR drawing averaged 2x2); text is blended from a full-size mask and matches
    assert psnr(ref.u, yuv.u) > 22 and psnr(ref.v, yuv.v) > 22
    assert psnr(ref.u[4:15, 5:20], yuv.u[4:15, 5:20]) > 30    # status text ("out"), left of any box
    # The alarm-red box really is red in the YUV drawing
    b, g, r = to_bgr(yuv)[60, 200].astype(int)
    assert r > 180 and g < 80 and b < 80


def test_yuv420_planes_are_views_of_one_array():
    yuv = to_yuv(base_bgr())
    yuv.u[:] = 0
    assert (yuv.arr.reshape(-1)[W * H:W * H + (W // 2) * (H // 2)] == 0).all()
    c = yuv.copy()
    c.y[:] = 0
    assert yuv.y.any()


@pytest.mark.parametrize("rois", [None, [(40, 30, 80, 80)]])
def test_encoder_takes_bgr_and_yuv(tmp_path, rois):
    out = tmp_path / "out.ts"
    e = Encoder(str(out), W, H, 10)
    bgr = base_bgr()
    for i in range(20):
        img = to_yuv(bgr) if i % 2 else bgr     # switching mid-stream is allowed
        e.encode(img, i / 10, rois)
        assert e.conv_s > 0
    e.close()
    with av.open(str(out)) as c:
        frames = list(c.decode(video=0))
    assert len(frames) == 20
    got = frames[-1].to_ndarray(format="bgr24").astype(int)
    assert np.abs(got - bgr.astype(int)).mean() < 6


def test_source_frame_scales_to_detector_size():
    f = av.VideoFrame.from_ndarray(np.zeros((1080, 1920, 3), np.uint8), format="bgr24").reformat(format="yuv420p")
    sf = SourceFrame(f)
    assert sf.shape == (1080, 1920, 3)
    assert sf.for_detector(640).shape == (360, 640, 3)
    assert sf.bgr().shape == (1080, 1920, 3)
    small = av.VideoFrame.from_ndarray(np.zeros((480, 640, 3), np.uint8), format="bgr24").reformat(format="yuv420p")
    assert SourceFrame(small).for_detector(640).shape == (480, 640, 3)     # never upscaled


MODEL = ROOT / "worker" / "models" / "yolo11n_openvino_model"
SAMPLE = ROOT / "data" / "samples" / "worker.mp4"


@pytest.mark.skipif(not (MODEL.exists() and SAMPLE.exists()), reason="needs the model and sample video")
def test_detecting_on_scaled_copy_gives_same_boxes():
    from detector import YoloDetector
    det = YoloDetector(str(MODEL), conf=0.35)
    with av.open(str(SAMPLE)) as c:
        for frame in c.decode(video=0):
            full = det.detect(frame.to_ndarray(format="bgr24"))
            if len(full) >= 2:
                break
    sf = SourceFrame(frame)
    small = det.detect(sf.for_detector(640), out_size=(frame.width, frame.height))
    assert full and len(small) == len(full)
    for a in full:
        b = max(small, key=lambda d: -abs(d.x1 - a.x1) - abs(d.y2 - a.y2))
        assert b.cls == a.cls
        assert max(abs(a.x1 - b.x1), abs(a.y1 - b.y1), abs(a.x2 - b.x2), abs(a.y2 - b.y2)) < 6
