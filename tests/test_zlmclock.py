import struct

import zlmclock
from zlmclock import ZlmClock, _first_tfdt, _video_track


def box(kind, payload=b""):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def full(version=0, flags=0):
    return struct.pack(">I", (version << 24) | flags)


def trak(tid, handler, scale):
    tkhd = box(b"tkhd", full() + b"\0" * 8 + struct.pack(">I", tid) + b"\0" * 64)
    mdhd = box(b"mdhd", full() + b"\0" * 8 + struct.pack(">I", scale) + b"\0" * 8)
    hdlr = box(b"hdlr", full() + b"\0" * 4 + handler + b"\0" * 12)
    return box(b"trak", tkhd + box(b"mdia", mdhd + hdlr))


def traf(tid, t, version=1):
    tfhd = box(b"tfhd", full() + struct.pack(">I", tid))
    tfdt = box(b"tfdt", full(version) + (struct.pack(">Q", t) if version else struct.pack(">I", t)))
    return box(b"traf", tfhd + tfdt)


def test_fmp4_video_track_and_first_tfdt():
    moov = trak(1, b"soun", 44100) + trak(2, b"vide", 90000)
    assert _video_track(moov) == (2, 90000)
    moof = box(b"mfhd", full() + b"\0" * 4) + traf(1, 999) + traf(2, 525_672_000)
    assert _first_tfdt(moof, 2) / 90000 == 5840.8
    assert _first_tfdt(box(b"mfhd") + traf(2, 1000, version=0), 2) == 1000
    assert _first_tfdt(traf(1, 5), 2) is None


def test_stamps():
    c = ZlmClock("rtsp://127.0.0.1/live/ped")
    assert c.stamps(9000) == (None, None)               # base unknown
    c.base = 2 ** 32 - 9000                             # RTP timestamp wraps
    assert c.stamps(18000) == (9000, None)
    c.pair = ((2 ** 32 - 9000 + 18000) % 2 ** 32 - c.base, 100.0)   # pts 18000 <-> fMP4 100 s
    assert c.stamps(18000 + 90000)[1] == 101.0
    assert c.stamps(None) == (None, None)


def test_open_pins_base_only_when_no_keyframe_came_in_between(monkeypatch):
    seq = iter([500, 500])
    monkeypatch.setattr(zlmclock, "probe_rtsp", lambda url: next(seq))
    c = ZlmClock("rtsp://127.0.0.1/live/ped")
    assert c.open(lambda: "container") == "container" and c.base == 500

    seq = iter([500, 680, 680, 680])       # keyframe during the 1st open: closed and reopened
    closed = []
    monkeypatch.setattr(zlmclock, "probe_rtsp", lambda url: next(seq))
    containers = iter([type("C", (), {"close": lambda self: closed.append(1)})(), "second"])
    assert c.open(lambda: next(containers)) == "second" and c.base == 680 and closed == [1]


def test_open_without_probe_still_plays(monkeypatch):
    def fail(url):
        raise ConnectionError("refused")
    monkeypatch.setattr(zlmclock, "probe_rtsp", fail)
    c = ZlmClock("rtsp://127.0.0.1/live/ped")
    assert c.open(lambda: "container") == "container" and c.base is None


def test_fmp4_url():
    assert ZlmClock("rtsp://127.0.0.1:554/live/ped", "http://127.0.0.1/").fmp4_url == \
        "http://127.0.0.1/live/ped.live.mp4"
    assert ZlmClock("rtsp://127.0.0.1/live/ped").fmp4_url is None
