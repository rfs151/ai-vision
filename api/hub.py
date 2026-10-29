"""WebSocket hub: alarms / worker status to all browsers; live track boxes only to the browsers
that subscribed to a stream ({"sub": ["ped", ...]} from the client)."""
import asyncio
import json
import logging

from fastapi import WebSocket

log = logging.getLogger("hub")


class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.subs: dict[WebSocket, set[str]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket):
        self.clients.discard(ws)
        self.subs.pop(ws, None)

    def subscribe(self, ws: WebSocket, streams):
        self.subs[ws] = {str(s) for s in streams}

    def subscribed(self, stream):
        return any(stream in s for s in self.subs.values())

    async def send_stream(self, stream, kind, data):
        msg = json.dumps({"type": kind, "data": data}, separators=(",", ":"))
        for ws, streams in list(self.subs.items()):
            if stream in streams:
                try:
                    await ws.send_text(msg)
                except Exception:
                    self.disconnect(ws)

    async def broadcast(self, kind, data):
        msg = json.dumps({"type": kind, "data": data}, ensure_ascii=False, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def publish(self, kind, data):
        """Thread-safe: callable from sync routes (threadpool) as well as the event loop."""
        if self.loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self.loop.create_task(self.broadcast(kind, data))
        else:
            asyncio.run_coroutine_threadsafe(self.broadcast(kind, data), self.loop)


hub = Hub()
