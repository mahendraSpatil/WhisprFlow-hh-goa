from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.memory import MemoryStore, default_path, memory_enabled
from app.models.memory import Incident

router = APIRouter()


class Incidents(BaseModel):
    enabled: bool
    total: int
    incidents: list[Incident]


@router.get("/memory/incidents", response_model=Incidents)
async def list_incidents(limit: int = Query(500, ge=1, le=2000)) -> Incidents:
    """Remembered incidents, newest first, each with how many incidents share its signature."""
    if not memory_enabled():
        return Incidents(enabled=False, total=0, incidents=[])

    def read() -> Incidents:
        store = MemoryStore(default_path())
        return Incidents(enabled=True, total=store.count(), incidents=store.list_incidents(limit))

    return await asyncio.to_thread(read)
