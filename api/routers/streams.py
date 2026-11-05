"""Test streams (looped sample videos, scripts/streams.sh): list them and switch them on / off.

This service only writes the choice to data/streams.enabled; the pusher (`streams.sh run`, the
ai-vision-streams service or the demo container) applies it within 5 s. "pushing" comes from ZLM:
the live/<name> stream is online.
"""
import os
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api.config import settings
from api.routers.devices import online_set

router = APIRouter(prefix="/api/test-streams", tags=["test-streams"])
# Same rule as catalog() in streams.sh: "name file" lines, "#name file" = off
LINE = re.compile(r"^\s*(#?)\s*([A-Za-z0-9_-]+)\s+(\S+\.(?:mp4|mkv|mov|flv|ts))\s*$")


class Toggle(BaseModel):
    enabled: bool


def catalog():
    """[(name, file, on_in_conf)] in conf order."""
    try:
        text = settings.streams_conf.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [(m[2], m[3], not m[1]) for m in map(LINE.match, text.splitlines()) if m]


def selected(cat):
    """Names that should be pushed: the UI's choice if made, else the conf comments."""
    try:
        return set(settings.streams_selection.read_text(encoding="utf-8").split())
    except FileNotFoundError:
        return {name for name, _, on in cat if on}


def save(names):
    path = settings.streams_selection
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(f"{n}\n" for n in sorted(names)), encoding="utf-8")
    os.replace(tmp, path)   # atomic: streams.sh never reads a half-written file


async def listing(request):
    cat = catalog()
    on = selected(cat)
    online = await online_set(request)
    return [{"name": name, "file": file, "enabled": name in on, "pushing": ("live", name) in online}
            for name, file, _ in cat]


@router.get("")
async def list_streams(request: Request):
    return await listing(request)


@router.put("/{name}")
async def set_stream(name: str, body: Toggle, request: Request):
    cat = catalog()
    if name not in {n for n, _, _ in cat}:
        raise HTTPException(404, "not in scripts/streams.conf")
    on = selected(cat)
    (on.add if body.enabled else on.discard)(name)
    save(on)
    return await listing(request)
