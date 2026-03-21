from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.event_store import EventStore
from src.projections.compliance_audit import get_compliance_at, get_current_compliance
from src.projections.daemon import ProjectionDaemon


async def get_application_summary(dsn: str, application_id: str) -> dict[str, Any] | None:
    async with await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row) as conn:
        cur = await conn.execute(
            "SELECT * FROM application_summary WHERE application_id = %s",
            (application_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_audit_trail(store: EventStore, application_id: str, from_pos: int = 0, to_pos: int | None = None) -> list[dict]:
    stream_id = f"loan-{application_id}"
    events = await store.load_stream(stream_id, from_position=from_pos, to_position=to_pos)
    return [e.model_dump(mode="json") for e in events]


async def get_agent_session(store: EventStore, agent_id: str, session_id: str) -> list[dict]:
    events = await store.load_stream(f"agent-{agent_id}-{session_id}")
    return [e.model_dump(mode="json") for e in events]


async def get_compliance(dsn: str, application_id: str, as_of: datetime | None = None) -> dict[str, Any]:
    if as_of is None:
        return await get_current_compliance(dsn, application_id)
    return await get_compliance_at(dsn, application_id, as_of)


async def get_health(daemon: ProjectionDaemon) -> list[dict]:
    lags = await daemon.get_all_lags()
    return [lag.__dict__ for lag in lags]

