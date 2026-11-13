"""Delete old alarm snapshots and clips so the disk doesn't fill up.

Two limits, checked every hour (and once at startup):
  MEDIA_KEEP_DAYS  files older than this are deleted (default 7, 0 = no age limit)
  MEDIA_MAX_GB     if snapshots + clips together are still larger, delete the oldest
                   files until they fit (default 5, 0 = no size limit)
Alarm records stay in the database; only their snapshot / clip links are cleared.
"""
import asyncio
import logging
import os
import time
from pathlib import Path

from sqlalchemy import update

from api.config import settings
from api.db import SessionLocal
from api.models import Alarm

log = logging.getLogger("retention")
MEDIA_DIRS = ("alarms", "clips")
INTERVAL = 3600


def scan(data_dir: Path):
    """[(mtime, size, path)] of all media files, oldest first."""
    files = []
    for d in MEDIA_DIRS:
        for root, _, names in os.walk(data_dir / d):
            for n in names:
                p = Path(root) / n
                try:
                    st = p.stat()
                except FileNotFoundError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
    files.sort()
    return files


def pick(files, keep_days, max_bytes, now):
    """Files to delete: older than keep_days, then oldest-first until the rest fit in max_bytes."""
    cutoff = now - keep_days * 86400 if keep_days > 0 else None
    doomed, kept = [], []
    for f in files:
        (doomed if cutoff is not None and f[0] < cutoff else kept).append(f)
    total = sum(f[1] for f in kept)
    if max_bytes > 0:
        i = 0
        while total > max_bytes and i < len(kept):
            doomed.append(kept[i])
            total -= kept[i][1]
            i += 1
    return doomed


def remove_empty_dirs(data_dir: Path):
    for d in MEDIA_DIRS:
        base = data_dir / d
        for root, dirs, files in os.walk(base, topdown=False):
            if Path(root) != base and not dirs and not files:
                try:
                    os.rmdir(root)
                except OSError:
                    pass


def clear_missing(data_dir: Path):
    """Clear snapshot / clip links whose file is gone (e.g. deleted by hand), so the UI doesn't
    offer recordings that 404. Returns the number of links cleared."""
    cleared = 0
    with SessionLocal() as db:
        for col in (Alarm.snapshot, Alarm.clip):
            rels = [r for (r,) in db.query(col).filter(col.isnot(None)).distinct()]
            gone = [r for r in rels if not (data_dir / r).exists()]
            for i in range(0, len(gone), 500):
                res = db.execute(update(Alarm).where(col.in_(gone[i:i + 500])).values({col.key: None}))
                cleared += res.rowcount
        db.commit()
    if cleared:
        log.info("cleared %d links to snapshot/clip files that no longer exist", cleared)
    return cleared


def clean(data_dir: Path = None, keep_days: float = None, max_gb: float = None, now: float = None):
    """One pass. Returns (files deleted, bytes freed)."""
    data_dir = data_dir or settings.data_dir
    keep_days = settings.media_keep_days if keep_days is None else keep_days
    max_gb = settings.media_max_gb if max_gb is None else max_gb
    doomed = pick(scan(data_dir), keep_days, int(max_gb * 1024 ** 3), now or time.time())
    clear_missing(data_dir)
    if not doomed:
        return 0, 0
    freed, rels = 0, []
    for _, size, p in doomed:
        try:
            p.unlink()
        except FileNotFoundError:
            continue
        freed += size
        rels.append(p.relative_to(data_dir).as_posix())
    remove_empty_dirs(data_dir)
    with SessionLocal() as db:
        for i in range(0, len(rels), 500):
            chunk = rels[i:i + 500]
            db.execute(update(Alarm).where(Alarm.snapshot.in_(chunk)).values(snapshot=None))
            db.execute(update(Alarm).where(Alarm.clip.in_(chunk)).values(clip=None))
        db.commit()
    log.info("deleted %d old snapshot/clip files, freed %.1f MB", len(rels), freed / 1e6)
    return len(rels), freed


async def loop(interval: float = INTERVAL):
    while True:
        try:
            await asyncio.to_thread(clean)
        except Exception:
            log.exception("media cleanup failed")
        try:
            from api import search
            n = await asyncio.to_thread(search.prune)
            if n:
                log.info("removed %d sightings older than %s days", n, search.settings.sighting_keep_days)
        except Exception:
            log.exception("sighting cleanup failed")
        await asyncio.sleep(interval)
