"""Live track boxes from workers (UDP) -> browsers that subscribed to that stream (WebSocket).

Workers send one small JSON datagram per detection (5/s per stream) to the API's own port number
over UDP. Bound to localhost by default (workers run on this machine); set TRACKS_BIND=0.0.0.0
for remote workers. Nothing is stored: a browser that is not looking at a stream gets nothing.
"""
import asyncio
import json
import logging
import os
from urllib.parse import urlparse

from api.config import settings
from api.hub import hub

log = logging.getLogger("tracks")


_transport = None
_workers: dict[str, tuple] = {}     # stream -> UDP address its worker sends track boxes from


def send_command(stream, cmd):
    """Tell a worker something right away (it listens on the socket it sends track boxes from):
    "reload" = fetch rules / video features now, "keyframe" = a viewer starts playing.
    Returns False if that worker has not sent anything yet (it will poll within 5 s anyway)."""
    addr = _workers.get(stream)
    if not (_transport and addr):
        return False
    _transport.sendto(json.dumps({"cmd": cmd}).encode(), addr)
    return True


class TracksProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        try:
            msg = json.loads(data)
            stream = msg["s"]
        except (ValueError, KeyError, TypeError):
            return
        _workers[stream] = addr
        if hub.subscribed(stream):
            asyncio.get_running_loop().create_task(hub.send_stream(stream, "tracks", msg))


async def start():
    host = os.getenv("TRACKS_BIND", "127.0.0.1")
    port = urlparse(settings.api_url).port or 8090
    try:
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            TracksProtocol, local_addr=(host, port))
    except OSError as e:          # overlays are optional: the rest of the service works without them
        log.warning("track boxes disabled, cannot bind udp %s:%d: %s", host, port, e)
        return None
    log.info("track boxes: udp %s:%d", host, port)
    global _transport
    _transport = transport
    return transport
