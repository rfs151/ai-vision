import json
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import video  # noqa: E402
from apiclient import ApiClient  # noqa: E402
from video import GOP_FIXED, GOP_LONG, VideoCtl  # noqa: E402


class FakeEnc:
    def __init__(self):
        self.gop_s, self.keys = None, []

    def request_keyframe(self, reason):
        self.keys.append(reason)


T0, T1, T2, T3 = ([type("V", (), {"box": (10, 10, 60, 110)})()] * n for n in range(4))


def test_off_keeps_fixed_interval_and_ignores_events():
    v, e = VideoCtl(), FakeEnc()
    v.viewer_joined()
    v.apply(e, T3, 100.0, 640, 480)
    assert e.gop_s == GOP_FIXED and e.keys == []


def test_dynamic_keyframes(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(video.time, "monotonic", lambda: now[0])
    v, e = VideoCtl(), FakeEnc()
    v.set_config({"dyn_gop": True})
    v.apply(e, T2, None, 640, 480)                     # first targets ever: no extra keyframe
    assert e.gop_s == GOP_LONG and e.keys == []
    now[0] += 1
    v.apply(e, T0, None, 640, 480)
    now[0] += 1.5
    v.apply(e, T1, None, 640, 480)                     # 2.5 s since targets were last seen < QUIET_SEC
    now[0] += 5
    v.apply(e, T0, None, 640, 480)
    v.apply(e, T1, None, 640, 480)                     # 5 s without targets, then some
    assert e.keys == ["activity"]
    v.apply(e, T1, 42.0, 640, 480)
    v.apply(e, T1, 42.0, 640, 480)                     # same alarm seen again: once
    v.viewer_joined()
    v.apply(e, T1, 42.0, 640, 480)
    assert e.keys == ["activity", "alarm", "viewer"]


def test_api_commands_reach_worker_over_udp():
    """The API answers on the socket the worker sends track boxes from."""
    api = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    api.bind(("127.0.0.1", 0))
    port = api.getsockname()[1]
    c = ApiClient(f"http://127.0.0.1:{port}", "s1")
    viewers = []
    c.on_viewer = lambda: viewers.append(1)
    threading.Thread(target=c._command_loop, daemon=True).start()
    c.send_tracks({"s": "s1"})
    data, addr = api.recvfrom(100)
    assert json.loads(data) == {"s": "s1"}
    api.sendto(b'{"cmd": "keyframe"}', addr)
    api.sendto(b'{"cmd": "reload"}', addr)
    # Someone else (not the API address) cannot send commands: the socket is connected
    other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    other.sendto(b'{"cmd": "keyframe"}', addr)
    assert c._wake.wait(2)
    time.sleep(0.1)
    assert viewers == [1]


class View:
    def __init__(self, box):
        self.box = box


def test_roi_boxes_margin_clip_and_order():
    views = [View((100, 100, 150, 200)), View((0, 0, 10, 10)), View((600, 400, 700, 480))]
    r = video.roi_boxes(views, 640, 480)
    # 50x100 box + 20 % per side; the one at the corner clipped to the frame; 10x10 dropped; largest first
    assert r == [(90, 80, 70, 140), (580, 384, 60, 96)]


def test_quality_probe_measures_each_output_on_the_right_frame():
    import numpy as np
    from encoder import Encoder
    import tempfile, os
    W, H = 320, 240
    base = np.random.default_rng(0).integers(0, 255, (H, W, 3), dtype=np.uint8)
    imgs = [np.roll(base, i * 4, axis=1) for i in range(40)]
    d = tempfile.mkdtemp()
    roi = [(80, 60, 120, 100)]
    main = Encoder(os.path.join(d, "a.ts"), W, H, 10)
    ref = Encoder(os.path.join(d, "b.ts"), W, H, 10)
    probe = video.QualityProbe()
    for i, img in enumerate(imgs):
        probe.feed(img, roi, {"main": main.encode(img, i / 10, roi), "ref": ref.encode(img, i / 10)})
    s = probe.stats()
    assert s["main"]["samples"] == 4 and s["ref"]["samples"] == 4
    # Right frame: a neighbouring (shifted) noise frame would score ~8 dB
    assert s["ref"]["psnr_roi"] > 12 and s["ref"]["psnr_bg"] > 12
    assert s["main"]["psnr_roi"] > s["ref"]["psnr_roi"] + 2      # ROI on main only
