import asyncio
import json
import os
import sys
import tempfile
import time

import pytest

_tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = _tmp
os.environ["DB_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["ZLM_SECRET"] = "x"

from fastapi.testclient import TestClient  # noqa: E402

import api.main as main  # noqa: E402
from api.manager import WorkerInfo, manager  # noqa: E402


class FakeZLM:
    def __init__(self):
        self.online = set()
        self.proxies = []

    async def media_list(self, app=None, stream=None):
        return [{"app": a, "stream": s} for a, s in self.online]

    async def add_proxy(self, app, stream, url):
        self.proxies.append((app, stream, url))
        return {"code": 0}

    async def del_proxy(self, app, stream):
        pass

    async def call(self, api, **kw):
        return {"code": 0}

    async def close(self):
        pass


@pytest.fixture()
def client(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "ZLM", FakeZLM)
    monkeypatch.setattr(manager, "start", lambda dev, **kw: calls.append(("start", dev.stream)))
    monkeypatch.setattr(manager, "stop", lambda s, **kw: calls.append(("stop", s)))
    with TestClient(main.app) as c:
        c.calls = calls
        yield c


def changed(c, app, stream, regist, schema="rtsp"):
    return c.post("/index/hook/on_stream_changed",
                  json={"app": app, "stream": stream, "regist": regist, "schema": schema})


def test_stream_online_auto_registers_and_starts_worker(client):
    assert changed(client, "live", "cam1", True).json()["code"] == 0
    assert ("start", "cam1") in client.calls
    devs = client.get("/api/devices").json()
    assert [d["stream"] for d in devs] == ["cam1"]
    assert devs[0]["play"]["ai"]["fmp4"].endswith("/ai/cam1.live.mp4")
    assert devs[0]["play"]["ai"]["fmp4"].startswith("http://testserver")
    # Through the HTTPS gateway: same-origin URLs, no ZLM port
    via = client.get("/api/devices", headers={"X-Forwarded-Proto": "https",
                                              "X-Forwarded-Host": "vision.example.com"}).json()[0]["play"]
    assert via["ai"]["fmp4"] == "https://vision.example.com/ai/cam1.live.mp4"
    assert via["source"]["webrtc"].startswith("https://vision.example.com/index/api/webrtc?")


def test_hook_filters_other_schemas_and_ai_app(client):
    changed(client, "live", "cam2", True, schema="rtmp")
    changed(client, "ai", "cam2", True)
    assert client.calls == []
    assert all(d["stream"] != "cam2" for d in client.get("/api/devices").json())


def test_reconcile_registers_streams_without_hook(client, monkeypatch):
    """Hooks can be lost: the reconcile loop must register new live/* streams by itself."""
    import asyncio
    client.app.state.zlm.online = {("live", "cam5"), ("ai", "cam5"), ("other", "x")}
    reconciled = []
    monkeypatch.setattr(main.manager, "reconcile", lambda devs, online: reconciled.append(sorted(d.stream for d in devs)))
    asyncio.run(main.reconcile_once(client.app))
    streams = [d["stream"] for d in client.get("/api/devices").json()]
    assert "cam5" in streams and "x" not in streams     # only auto apps; ai/ never
    assert sum(s == "cam5" for s in streams) == 1
    assert "cam5" in reconciled[0]      # the new device is handed to the manager right away


def test_stream_offline_stops_worker(client):
    changed(client, "live", "cam3", True)
    changed(client, "live", "cam3", False)
    assert ("stop", "cam3") in client.calls


def test_create_device_with_camera_pulls_proxy(client):
    r = client.post("/api/devices", json={"name": "gate", "stream": "gate",
                                          "source_url": "rtsp://10.0.0.5/stream1"})
    assert r.status_code == 201
    assert ("live", "gate", "rtsp://10.0.0.5/stream1") in main.app.state.zlm.proxies
    assert client.post("/api/devices", json={"name": "dup", "stream": "gate"}).status_code == 409
    assert client.post("/api/devices", json={"name": "bad", "stream": "a/b"}).status_code == 422


def test_rules_validation_and_versioning(client):
    dev = client.post("/api/devices", json={"name": "r", "stream": "rules1"}).json()
    v0 = client.get("/api/internal/rules/rules1").json()["version"]
    bad = {"zones": [{"id": "z", "polygon": [[0, 0], [1, 1]]}]}            # < 3 points
    assert client.put(f"/api/devices/{dev['id']}/rules", json=bad).status_code == 422
    good = {"cooldown_sec": 10, "zones": [{"id": "z", "polygon": [[0, 0], [1, 0], [1, 1]], "dwell_sec": 3}],
            "lines": [{"id": "l", "p1": [0.5, 0], "p2": [0.5, 1], "alarm_on": "in"}]}
    assert client.put(f"/api/devices/{dev['id']}/rules", json=good).status_code == 200
    got = client.get("/api/internal/rules/rules1").json()
    assert got["version"] == v0 + 1
    assert got["rules"]["zones"][0]["dwell_sec"] == 3 and got["rules"]["cooldown_sec"] == 10


def post_alarm(c, stream, typ="intrusion", with_img=True):
    ev = {"stream": stream, "type": typ, "rule_id": "z1", "rule_name": "zone", "t": 12.3,
          "track_id": 7, "cls": "person", "score": 0.8, "box": [1, 2, 3, 4], "detail": {}, "alarm": True}
    files = {"snapshot": ("s.jpg", b"\xff\xd8fakejpeg", "image/jpeg")} if with_img else None
    return c.post("/api/internal/alarms", data={"event": json.dumps(ev)}, files=files)


def test_alarm_ingest_list_snapshot_status_stats(client):
    client.post("/api/devices", json={"name": "a", "stream": "al1"})
    with client.websocket_connect("/ws") as ws:
        r = post_alarm(client, "al1")
        assert r.status_code == 201
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "alarm" and msg["data"]["stream"] == "al1"
    post_alarm(client, "al1", typ="dwell", with_img=False)

    page = client.get("/api/alarms", params={"stream": "al1"}).json()
    assert page["total"] == 2 and page["items"][0]["type"] == "dwell"          # newest first
    intr = client.get("/api/alarms", params={"stream": "al1", "type": "intrusion"}).json()["items"][0]
    assert client.get(intr["snapshot_url"]).content == b"\xff\xd8fakejpeg"

    assert client.patch(f"/api/alarms/{intr['id']}", json={"status": "ack"}).json()["status"] == "ack"
    assert client.patch(f"/api/alarms/{intr['id']}", json={"status": "bogus"}).status_code == 422
    st = client.get("/api/alarms/stats").json()
    assert st["by_device"]["al1"] == 2 and st["by_type"]["intrusion"] >= 1

    assert post_alarm(client, "unknown").status_code == 404


def test_heartbeat_unmanaged_worker(client):
    client.post("/api/devices", json={"name": "h", "stream": "hb1"})
    assert client.post("/api/internal/heartbeat/hb1", json={"out_fps": 10}).json() == {"managed": False}


def test_manager_restarts_crashed_worker_with_backoff():
    from api.manager import WorkerManager
    m = WorkerManager()
    w = WorkerInfo("live", "crash", 1, [sys.executable, "-c", "import sys; sys.exit(3)"])
    m.workers["crash"] = w
    m._spawn(w)
    w.proc.wait()
    m._check()
    assert w.state == "backoff" and w.restarts == 1
    first_pid = w.proc.pid
    w.next_start = time.time() - 1          # backoff elapsed
    m._check()
    assert w.state == "starting" and w.proc.pid != first_pid
    w.proc.wait()
    m._check()
    assert w.restarts == 2 and w.next_start - time.time() > 3   # 2^2 = 4 s
    m.stop("crash")
    assert w.state == "stopped"


def test_clip_upload_links_all_alarms(client):
    client.post("/api/devices", json={"name": "c", "stream": "cl1"})
    ids = [post_alarm(client, "cl1").json()["id"] for _ in range(2)]
    with client.websocket_connect("/ws") as ws:
        r = client.post("/api/internal/clips", data={"alarm_ids": json.dumps(ids), "start": "1793280000"},
                        files={"clip": ("clip.mp4", b"fakemp4", "video/mp4")})
        assert r.status_code == 201 and r.json()["alarms"] == 2
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "alarm_clip" and sorted(msg["data"]["ids"]) == sorted(ids)
    for i in ids:
        a = client.get(f"/api/alarms/{i}").json()
        assert a["clip_url"] == r.json()["clip_url"] and a["clip_url"].startswith("/media/clips/cl1/")
    assert client.get(r.json()["clip_url"]).content == b"fakemp4"
    assert client.post("/api/internal/clips", data={"alarm_ids": "[999999]"},
                       files={"clip": ("c.mp4", b"x", "video/mp4")}).status_code == 404


# ---- push (WeCom / DingTalk) ----

@pytest.fixture()
def robots(monkeypatch):
    """Fake robot endpoints: records every request, answers like the real ones."""
    import httpx
    from api.notify import notifier
    sent = []

    def handler(req):
        sent.append((str(req.url), json.loads(req.content)))
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    monkeypatch.setattr(notifier, "http", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(notifier, "sent", __import__("collections").deque())
    yield sent
    notifier.cfg = __import__("api.notify", fromlist=["NotifyConfig"]).NotifyConfig()


def test_notify_config_masks_secret_and_validates(client, robots):
    cfg = {"enabled": True, "wecom_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k",
           "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=t", "dingtalk_secret": "SEC"}
    r = client.put("/api/notify", json=cfg).json()
    assert r["dingtalk_secret"] == "******"
    cfg["dingtalk_secret"] = "******"                      # saving again without touching it
    client.put("/api/notify", json=cfg)
    from api.notify import notifier
    assert notifier.cfg.dingtalk_secret == "SEC"
    assert client.put("/api/notify", json={"wecom_webhook": "ftp://x"}).status_code == 422

    res = client.post("/api/notify/test").json()["result"]
    assert res == {"wecom": True, "dingtalk": True}
    urls = [u for u, _ in robots]
    assert any("timestamp=" in u and "sign=" in u for u in urls if "dingtalk" in u)   # 加签


def test_alarm_is_pushed_with_image_and_rate_limited(client, robots):
    client.post("/api/devices", json={"name": "门口", "stream": "nt1"})
    client.put("/api/notify", json={"enabled": True, "rate_per_min": 2,
                                    "wecom_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k"})
    for _ in range(3):
        post_alarm(client, "nt1")
    time.sleep(0.3)        # pushes run as background tasks
    kinds = [b["msgtype"] for _, b in robots]
    assert kinds == ["markdown", "image", "markdown", "image"]       # third alarm over the limit
    md = robots[0][1]["markdown"]["content"]
    assert "区域入侵告警" in md and "门口" in md
    from api.notify import notifier
    assert notifier.dropped >= 1

    robots.clear()
    client.put("/api/notify", json={"enabled": True, "types": ["dwell"],
                                    "wecom_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k"})
    notifier.sent.clear()
    post_alarm(client, "nt1")                                        # intrusion: filtered out
    time.sleep(0.2)
    assert robots == []


def test_track_boxes_only_reach_subscribers(client):
    """UDP datagram from a worker -> WebSocket clients subscribed to that stream."""
    import asyncio
    from api import tracks
    from api.hub import hub
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        a.send_text(json.dumps({"sub": ["tk1"]}))
        b.send_text(json.dumps({"sub": ["other"]}))
        time.sleep(0.1)
        msg = {"s": "tk1", "a": 1.0, "w": 640, "h": 480, "tr": [[3, "person", 0.9, .1, .2, .3, .4, 0, 0, 0]]}
        asyncio.run_coroutine_threadsafe(_deliver(tracks, msg), hub.loop).result(2)
        got = json.loads(a.receive_text())
        assert got == {"type": "tracks", "data": msg}
        assert hub.subscribed("tk1") and not hub.subscribed("nobody")


async def _deliver(tracks, msg):
    tracks.TracksProtocol().datagram_received(json.dumps(msg).encode(), ("127.0.0.1", 1))
    tracks.TracksProtocol().datagram_received(b"not json", ("127.0.0.1", 1))
    await asyncio.sleep(0.05)
