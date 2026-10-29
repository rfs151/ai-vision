"""Alarm push to WeCom (企业微信) and DingTalk (钉钉) group robots.

WeCom:    markdown text, then the snapshot as an image message (sent as base64, so it works without
          a public URL for this server).
DingTalk: markdown; DingTalk can only show images by URL, so the snapshot appears only when
          public_url is set. Optional "加签" secret (HMAC-SHA256).

Sending never blocks alarm ingest: each alarm becomes an asyncio task. Both robots allow
20 messages per minute; above the configured rate, alarms are counted as dropped, not queued
(a burst of old alarms arriving minutes late is worse than a summary in the UI).
"""
import asyncio
import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
from collections import deque
from datetime import datetime

import httpx
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("notify")

TYPE_LABELS = {"intrusion": "区域入侵", "dwell": "逗留超时", "crowd": "人数超限",
               "line_in": "越线(进)", "line_out": "越线(出)"}
MASK = "******"
WECOM_IMAGE_MAX = 2 * 1024 * 1024      # WeCom image message limit (before base64)


class NotifyConfig(BaseModel):
    enabled: bool = False
    types: list[str] = Field(default_factory=list)       # alarm types to push; empty = all
    wecom_webhook: str = ""
    wecom_image: bool = True                              # also send the snapshot
    dingtalk_webhook: str = ""
    dingtalk_secret: str = ""
    public_url: str = ""          # e.g. http://1.2.3.4:8090 -> links + DingTalk images
    rate_per_min: int = Field(20, ge=1, le=60)

    @field_validator("wecom_webhook", "dingtalk_webhook", "public_url")
    @classmethod
    def _url(cls, v):
        v = v.strip()
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return v.rstrip("/") if v else v

    def masked(self):
        d = self.model_dump()
        if d["dingtalk_secret"]:
            d["dingtalk_secret"] = MASK
        return d


def alarm_text(a, device_name):
    """(title, markdown lines) shared by both robots."""
    label = TYPE_LABELS.get(a.type, a.type)
    title = f"{label}告警 · {device_name}"
    d = a.detail or {}
    extra = (f"停留 {d['seconds']} 秒" if d.get("seconds") is not None else
             f"{d['count']} 人（上限 {d['max']}）" if d.get("count") is not None else
             f"累计 进 {d['in']} / 出 {d['out']}" if d.get("in") is not None else "")
    lines = [f"设备：{device_name}（{a.stream}）", f"规则：{a.rule_name}"]
    if a.track_id:
        lines.append(f"目标：{a.cls} #{a.track_id}（置信度 {a.score:.2f}）")
    if extra:
        lines.append(f"详情：{extra}")
    t = a.created_at if isinstance(a.created_at, datetime) else datetime.fromisoformat(str(a.created_at))
    lines.append(f"时间：{t:%Y-%m-%d %H:%M:%S}")
    return title, lines


class Notifier:
    def __init__(self):
        self.cfg = NotifyConfig()
        self.http = None
        self.sent = deque()             # send times within the last minute (rate limit)
        self.dropped = 0
        self.last = {}                  # channel -> {"ok", "time", "msg"}
        self.tasks = set()

    def client(self):
        if self.http is None:
            # trust_env=False: robots are reached directly, never through a proxy from env vars
            self.http = httpx.AsyncClient(timeout=10, trust_env=False)
        return self.http

    async def close(self):
        if self.http:
            await self.http.aclose()
            self.http = None

    def status(self):
        return {"dropped": self.dropped, "last": self.last}

    # ---- entry point from alarm ingest ----
    def alarm(self, a, device_name, snapshot_path=None):
        c = self.cfg
        if not c.enabled or not (c.wecom_webhook or c.dingtalk_webhook):
            return
        if c.types and a.type not in c.types:
            return
        now = time.monotonic()
        while self.sent and now - self.sent[0] > 60:
            self.sent.popleft()
        if len(self.sent) >= c.rate_per_min:
            self.dropped += 1
            log.warning("push rate limit (%d/min): alarm %s not pushed", c.rate_per_min, a.id)
            return
        self.sent.append(now)
        task = asyncio.get_running_loop().create_task(self.send_alarm(a, device_name, snapshot_path))
        self.tasks.add(task)                    # keep a reference until done
        task.add_done_callback(self.tasks.discard)

    async def send_alarm(self, a, device_name, snapshot_path=None):
        title, lines = alarm_text(a, device_name)
        link = f"{self.cfg.public_url}/#/alarms?id={a.id}" if self.cfg.public_url else ""
        image_url = f"{self.cfg.public_url}{a.snapshot_url}" if self.cfg.public_url and a.snapshot_url else ""
        jpg = None
        if snapshot_path and self.cfg.wecom_image:
            try:
                jpg = snapshot_path.read_bytes()
            except OSError:
                pass
        await asyncio.gather(self.wecom(title, lines, link, jpg), self.dingtalk(title, lines, link, image_url))

    # ---- channels ----
    def _record(self, channel, ok, msg):
        self.last[channel] = {"ok": ok, "time": datetime.now().isoformat(timespec="seconds"), "msg": msg}
        (log.info if ok else log.warning)("%s push %s: %s", channel, "ok" if ok else "failed", msg)
        return ok

    async def _post(self, channel, url, body):
        try:
            r = await self.client().post(url, json=body)
            d = r.json()
        except Exception as e:
            return self._record(channel, False, f"{type(e).__name__}: {e}")
        if d.get("errcode") != 0:               # both robots answer {"errcode": 0, "errmsg": "ok"}
            return self._record(channel, False, f"errcode {d.get('errcode')}: {d.get('errmsg')}")
        return self._record(channel, True, "ok")

    async def wecom(self, title, lines, link="", jpg=None):
        url = self.cfg.wecom_webhook
        if not url:
            return None
        md = f"### <font color=\"warning\">{title}</font>\n" + "\n".join(f"> {l}" for l in lines)
        if link:
            md += f"\n\n[查看详情]({link})"
        ok = await self._post("wecom", url, {"msgtype": "markdown", "markdown": {"content": md}})
        if ok and jpg and len(jpg) <= WECOM_IMAGE_MAX:
            ok = await self._post("wecom", url, {"msgtype": "image", "image": {
                "base64": base64.b64encode(jpg).decode(), "md5": hashlib.md5(jpg).hexdigest()}})
        return ok

    def dingtalk_url(self):
        url, secret = self.cfg.dingtalk_webhook, self.cfg.dingtalk_secret
        if not secret:
            return url
        ts = str(int(time.time() * 1000))
        sign = base64.b64encode(hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest())
        return f"{url}&timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"

    async def dingtalk(self, title, lines, link="", image_url=""):
        if not self.cfg.dingtalk_webhook:
            return None
        text = f"### {title}\n\n" + "\n\n".join(f"- {l}" for l in lines)
        if image_url:
            text += f"\n\n![snapshot]({image_url})"
        if link:
            text += f"\n\n[查看详情]({link})"
        return await self._post("dingtalk", self.dingtalk_url(),
                                {"msgtype": "markdown", "markdown": {"title": title, "text": text}})

    async def test(self):
        """Send a test message to every configured channel; returns {channel: ok}."""
        title = "测试消息 · AI 视频分析告警平台"
        lines = ["这是一条测试消息，收到说明推送配置正确。", f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}"]
        link = f"{self.cfg.public_url}/#/alarms" if self.cfg.public_url else ""
        res = {}
        if self.cfg.wecom_webhook:
            res["wecom"] = await self.wecom(title, lines, link)
        if self.cfg.dingtalk_webhook:
            res["dingtalk"] = await self.dingtalk(title, lines, link)
        return res


notifier = Notifier()
