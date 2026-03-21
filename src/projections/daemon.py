from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.event_store import EventStore
from src.models.events import StoredEvent
from src.projections.base import Projection, ProjectionLag


@dataclass(frozen=True)
class ProjectionHealth:
    name: str
    lag: ProjectionLag


class ProjectionDaemon:
    def __init__(self, store: EventStore, projections: list[Projection]) -> None:
        self._store = store
        self._projections = {p.name: p for p in projections}
        self._running = False

    async def run_forever(self, poll_interval_ms: int = 100) -> None:
        self._running = True
        while self._running:
            await self._process_batch()
            await asyncio.sleep(poll_interval_ms / 1000)

    async def run_once(self, batch_size: int = 200) -> None:
        await self._process_batch(batch_size=batch_size)

    def stop(self) -> None:
        self._running = False

    async def _ensure_checkpoint_row(self, conn: psycopg.AsyncConnection, name: str) -> None:
        await conn.execute(
            "INSERT INTO projection_checkpoints (projection_name, last_position) "
            "VALUES (%s, 0) "
            "ON CONFLICT (projection_name) DO NOTHING",
            (name,),
        )

    async def _get_checkpoint(self, conn: psycopg.AsyncConnection, name: str) -> int:
        await self._ensure_checkpoint_row(conn, name)
        cur = await conn.execute(
            "SELECT last_position FROM projection_checkpoints WHERE projection_name = %s",
            (name,),
        )
        row = await cur.fetchone()
        return int(row["last_position"])

    async def _set_checkpoint(self, conn: psycopg.AsyncConnection, name: str, pos: int) -> None:
        await conn.execute(
            "UPDATE projection_checkpoints SET last_position = %s, updated_at = NOW() "
            "WHERE projection_name = %s",
            (pos, name),
        )

    async def _latest_global_position(self, conn: psycopg.AsyncConnection) -> int:
        cur = await conn.execute("SELECT COALESCE(MAX(global_position), 0) AS max_pos FROM events")
        row = await cur.fetchone()
        return int(row["max_pos"])

    async def _process_batch(self, batch_size: int = 200) -> None:
        async with await psycopg.AsyncConnection.connect(self._store._dsn, row_factory=dict_row) as conn:  # noqa: SLF001
            async with conn.transaction():
                checkpoints = {name: await self._get_checkpoint(conn, name) for name in self._projections}
                from_pos = min(checkpoints.values(), default=0)

                cur = await conn.execute(
                    "SELECT event_id, stream_id, stream_position, global_position, event_type, event_version, payload, metadata, recorded_at "
                    "FROM events WHERE global_position > %s "
                    "ORDER BY global_position ASC "
                    "LIMIT %s",
                    (from_pos, batch_size),
                )

                events: list[StoredEvent] = [StoredEvent(**row) async for row in cur]
                if not events:
                    return

                # Route each event to projections that (a) subscribe and (b) haven't processed it yet.
                for ev in events:
                    for name, proj in self._projections.items():
                        if ev.event_type not in proj.subscribed_event_types:
                            continue
                        if checkpoints[name] >= ev.global_position:
                            continue
                        await proj.handle(conn, ev)
                        checkpoints[name] = ev.global_position
                        await self._set_checkpoint(conn, name, checkpoints[name])

    async def get_lag(self, projection_name: str) -> ProjectionLag:
        async with await psycopg.AsyncConnection.connect(self._store._dsn, row_factory=dict_row) as conn:  # noqa: SLF001
            latest = await self._latest_global_position(conn)
            last = await self._get_checkpoint(conn, projection_name)
            return ProjectionLag(
                projection_name=projection_name,
                last_processed_global_position=last,
                latest_global_position=latest,
                lag_events=max(0, latest - last),
            )

    async def get_all_lags(self) -> list[ProjectionLag]:
        async with await psycopg.AsyncConnection.connect(self._store._dsn, row_factory=dict_row) as conn:  # noqa: SLF001
            latest = await self._latest_global_position(conn)
            out: list[ProjectionLag] = []
            for name in self._projections:
                last = await self._get_checkpoint(conn, name)
                out.append(
                    ProjectionLag(
                        projection_name=name,
                        last_processed_global_position=last,
                        latest_global_position=latest,
                        lag_events=max(0, latest - last),
                    )
                )
            return out

