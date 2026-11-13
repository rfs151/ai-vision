"""AI 查询: search people / vehicles by one sentence (api/search.py)."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from api import search as engine
from api.db import get_db

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("")
async def search(q: str = Query(..., min_length=1, max_length=200), db: Session = Depends(get_db)):
    q = q.strip()
    if not q:
        raise HTTPException(400, "empty question")
    # LLM call + CLIP text encoder take 0.1-3 s: keep the event loop free (video overlays, websockets)
    return await asyncio.to_thread(engine.search, db, q)


@router.get("/status")
def status(db: Session = Depends(get_db)):
    return engine.status(db)
