"""Minimal ZLMediaKit HTTP API client."""
import logging

import httpx

from api.config import settings

log = logging.getLogger("zlm")


class ZLMError(Exception):
    pass


class ZLM:
    def __init__(self, base=settings.zlm_api, secret=settings.zlm_secret):
        self.base = base.rstrip("/")
        self.secret = secret
        # trust_env=False: never route local ZLM calls through an http proxy from env vars
        self.client = httpx.AsyncClient(timeout=5, trust_env=False)

    async def call(self, api, **params):
        params["secret"] = self.secret
        r = await self.client.get(f"{self.base}/index/api/{api}", params=params)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise ZLMError(f"{api}: {data.get('msg')} (code {data.get('code')})")
        return data

    async def media_list(self, app=None, stream=None):
        params = {"schema": "rtsp"}
        if app:
            params["app"] = app
        if stream:
            params["stream"] = stream
        return (await self.call("getMediaList", **params)).get("data") or []

    async def is_online(self, app, stream):
        return bool(await self.media_list(app, stream))

    async def add_proxy(self, app, stream, url):
        """Pull a camera into ZLM as <app>/<stream>. retry_count=-1: retry forever."""
        return await self.call("addStreamProxy", vhost="__defaultVhost__", app=app, stream=stream,
                               url=url, retry_count=-1, rtp_type=0, timeout_sec=10,
                               enable_hls=0, enable_mp4=0)

    async def del_proxy(self, app, stream):
        try:
            return await self.call("delStreamProxy", key=f"__defaultVhost__/{app}/{stream}")
        except ZLMError as e:
            log.info("delStreamProxy %s/%s: %s", app, stream, e)

    async def close(self):
        await self.client.aclose()


def play_urls(app, stream, host="127.0.0.1"):
    hp, rp = settings.play_http_port, settings.play_rtsp_port
    http = f"http://{host}" + (f":{hp}" if hp != 80 else "")
    rtsp = f"rtsp://{host}" + (f":{rp}" if rp != 554 else "")
    return {
        "webrtc": f"{http}/index/api/webrtc?app={app}&stream={stream}&type=play",
        "fmp4": f"{http}/{app}/{stream}.live.mp4",
        "flv": f"{http}/{app}/{stream}.live.flv",
        "hls": f"{http}/{app}/{stream}/hls.m3u8",
        "rtsp": f"{rtsp}/{app}/{stream}",
    }
