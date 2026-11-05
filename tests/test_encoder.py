import re
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
from encoder import Encoder  # noqa: E402

W, H, FPS = 320, 240, 10


def frames(n):
    base = np.random.default_rng(0).integers(0, 255, (H, W, 3), dtype=np.uint8)
    return [np.roll(base, i * 4, axis=1) for i in range(n)]


def nal_types(path, tmp_path):
    """NAL unit types in stream order (7 SPS, 8 PPS, 5 IDR slice, 1 non-IDR slice)."""
    raw = tmp_path / "x.264"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-c", "copy", "-f", "h264", str(raw)], check=True)
    d = raw.read_bytes()
    return [d[m.end()] & 31 for m in re.finditer(b"\x00\x00\x01", d)]


def keyframe_times(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "packet=pts_time,flags",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout
    return [float(t) for t, f in (l.split(",")[:2] for l in out.split()) if "K" in f]


def test_forced_keyframe_and_sps_before_every_idr(tmp_path):
    out = tmp_path / "out.ts"
    e = Encoder(str(out), W, H, FPS)
    for i, img in enumerate(frames(50)):
        if i == 25:
            e.request_keyframe()
        e.encode(img, i / FPS)
    e.close()
    keys = keyframe_times(out)
    k0 = keys[0]
    # GOP 2 s -> 0, 2.0 ...; forced IDR at 2.5 s restarts the GOP -> next one at 4.5 s
    assert [round(k - k0, 1) for k in keys] == [0.0, 2.0, 2.5, 4.5]
    t = nal_types(out, tmp_path)
    idr_starts = [i for i, x in enumerate(t) if x == 5 and (i == 0 or t[i - 1] != 5)]
    assert len(idr_starts) == 4
    for i in idr_starts:                                       # SPS + PPS in front of every IDR
        assert {7, 8} <= set(t[max(0, i - 4):i])               # (AUD 9 / SEI 6 may sit in between)


def test_pts_never_goes_backwards(tmp_path):
    out = tmp_path / "out.ts"
    e = Encoder(str(out), W, H, FPS)
    times = [0, 0.1, 0.2, 0.2, 0.1, 0.3, 0.4]                  # repeated and backwards source pts
    for img, t in zip(frames(len(times)), times):
        e.encode(img, t)
    e.close()
    pts = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(out)],
                         capture_output=True, text=True, check=True).stdout.split()
    pts = [float(x.strip(",")) for x in pts]
    assert len(pts) == len(times)
    assert all(b > a for a, b in zip(pts, pts[1:]))


def test_segments_and_stats(tmp_path):
    seg = tmp_path / "ring"
    seg.mkdir()
    e = Encoder(str(tmp_path / "out.ts"), W, H, FPS, seg_pattern=f"{seg}/%03d.ts",
                seg_strftime=False)
    for i, img in enumerate(frames(60)):
        e.encode(img, i / FPS)
    s = e.stats()
    e.close()
    files = sorted(seg.glob("*.ts"))
    assert len(files) == 3                                     # 6 s in 2 s segments
    for f in files:
        assert keyframe_times(f), f"{f.name} has no keyframe"
        assert {7, 8} <= set(nal_types(f, tmp_path)[:4])       # each segment decodes on its own
    assert s["keyframes"] == 3 and s["out_kbps"] > 0 and s["enc_ms"] > 0 and s["qp"] > 0
    assert s["key_interval_s"] == 2.0 and s["key_reasons"] == {"gop": 3}


def test_keyframe_interval_can_change_while_encoding(tmp_path):
    out = tmp_path / "out.ts"
    e = Encoder(str(out), W, H, FPS)                           # 2 s
    for i, img in enumerate(frames(150)):
        if i == 30:
            e.gop_s = 5.0                                      # from 3 s on: every 5 s
        if i == 100:
            e.request_keyframe("viewer")                       # 10 s: on demand
        e.encode(img, i / FPS)
    s = e.stats()
    e.close()
    k0 = keyframe_times(out)[0]
    assert [round(k - k0, 1) for k in keyframe_times(out)] == [0.0, 2.0, 7.0, 10.0]
    assert s["key_reasons"] == {"gop": 3, "viewer": 1}


def region_psnr(path, src_frames, roi):
    """Luma PSNR of a decoded file vs the source frames, inside roi (x, y, w, h) and outside."""
    import av
    x, y, w, h = roi
    ins, outs = [], []
    for img, f in zip(src_frames, av.open(str(path)).decode(video=0)):
        ref = av.VideoFrame.from_ndarray(img, format="bgr24").reformat(format="yuv420p").to_ndarray()[:H]
        d = (f.to_ndarray()[:H].astype(np.float32) - ref) ** 2
        m = np.zeros(d.shape, bool)
        m[y:y + h, x:x + w] = True
        ins.append(d[m].mean())
        outs.append(d[~m].mean())
    p = lambda e: 10 * np.log10(255 ** 2 / np.mean(e))
    return p(ins), p(outs)


def test_roi_moves_quality_into_the_region(tmp_path):
    imgs = frames(40)                                          # noise: always at the bitrate cap
    roi = (80, 60, 120, 100)
    res = {}
    for name, rois in (("off", None), ("on", [roi])):
        e = Encoder(str(tmp_path / f"{name}.mp4"), W, H, FPS)
        for i, img in enumerate(imgs):
            e.encode(img, i / FPS, rois)
        e.close()
        res[name] = region_psnr(tmp_path / f"{name}.mp4", imgs, roi)
    assert res["on"][0] > res["off"][0] + 2                    # region: clearly better
    assert res["on"][1] < res["off"][1]                        # background pays for it
