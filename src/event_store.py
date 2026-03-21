from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import src.upcasting  # noqa: F401  (register default upcasters)

from src.models.events import (
    BaseEvent,
    OptimisticConcurrencyError,
    StoredEvent,
    StreamMetadata,
)
from src.upcasting.registry import DEFAULT_UPCASTERS, UpcasterRegistry


class EventStore:
    def __init__(
        self,
        *,
        dsn: str,
        upcasters: UpcasterRegistry | None = None,
        outbox_destination: str = "default",
    ) -> None:
        self._dsn = dsn
        self._upcasters = upcasters or DEFAULT_UPCASTERS
        self._outbox_destination = outbox_destination

    @classmethod
    def from_env(cls) -> "EventStore":
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL is required")
        return cls(dsn=dsn)

    async def append(
        self,
        stream_id: str,
        events: list[BaseEvent],
        expected_version: int,  # -1 new stream; N exact
        correlation_id: str | None = None,
        causation_id: str | None = None,
        aggregate_type: str = "Unknown",
    ) -> int:
        if not events:
            return await self.stream_version(stream_id)

        async with await psycopg.AsyncConnection.connect(self._dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                # Lock stream row to enforce optimistic concurrency.
                row = await conn.execute(
                    "SELECT stream_id, current_version, archived_at "
                    "FROM event_streams WHERE stream_id = %s FOR UPDATE",
                    (stream_id,),
                )
                stream = await row.fetchone()

                if stream is None:
                    if expected_version != -1:
                        raise OptimisticConcurrencyError(
                            stream_id=stream_id,
                            expected_version=expected_version,
                            actual_version=0,
                            message="Stream does not exist",
                        )
                    await conn.execute(
                        "INSERT INTO event_streams (stream_id, aggregate_type, current_version) "
                        "VALUES (%s, %s, 0)",
                        (stream_id, aggregate_type),
                    )
                    current_version = 0
                else:
                    if stream["archived_at"] is not None:
                        raise OptimisticConcurrencyError(
                            stream_id=stream_id,
                            expected_version=expected_version,
                            actual_version=int(stream["current_version"]),
                            message="Stream is archived",
                        )
                    current_version = int(stream["current_version"])

                if expected_version == -1:
                    if current_version != 0:
                        raise OptimisticConcurrencyError(
                            stream_id=stream_id,
                            expected_version=expected_version,
                            actual_version=current_version,
                        )
                else:
                    if current_version != expected_version:
                        raise OptimisticConcurrencyError(
                            stream_id=stream_id,
                            expected_version=expected_version,
                            actual_version=current_version,
                        )

                metadata_base: dict[str, Any] = {}
                if correlation_id is not None:
                    metadata_base["correlation_id"] = correlation_id
                if causation_id is not None:
                    metadata_base["causation_id"] = causation_id

                new_version = current_version
                for idx, ev in enumerate(events, start=1):
                    stream_position = current_version + idx
                    payload = ev.to_payload()
                    metadata = {
                        **metadata_base,
                        "event_type": ev.event_type,
                        "event_version": ev.event_version,
                    }
                    inserted = await conn.execute(
                        "INSERT INTO events (stream_id, stream_position, event_type, event_version, payload, metadata) "
                        "VALUES (%s, %s, %s, %s, %s, %s) "
                        "RETURNING event_id",
                        (
                            stream_id,
                            stream_position,
                            ev.event_type,
                            ev.event_version,
                            Jsonb(payload),
                            Jsonb(metadata),
                        ),
                    )
                    event_id = (await inserted.fetchone())["event_id"]
                    await conn.execute(
                        "INSERT INTO outbox (event_id, destination, payload) VALUES (%s, %s, %s)",
                        (event_id, self._outbox_destination, Jsonb(payload)),
                    )
                    new_version = stream_position

                await conn.execute(
                    "UPDATE event_streams SET current_version = %s WHERE stream_id = %s",
                    (new_version, stream_id),
                )
                return new_version

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[StoredEvent]:
        query = (
            "SELECT event_id, stream_id, stream_position, global_position, event_type, event_version, payload, metadata, recorded_at "
            "FROM events WHERE stream_id = %s AND stream_position > %s"
        )
        params: list[Any] = [stream_id, from_position]
        if to_position is not None:
            query += " AND stream_position <= %s"
            params.append(to_position)
        query += " ORDER BY stream_position ASC"

        async with await psycopg.AsyncConnection.connect(self._dsn, row_factory=dict_row) as conn:
            rows = await conn.execute(query, params)
            out: list[StoredEvent] = []
            async for r in rows:
                ev = StoredEvent(**r)
                out.append(self._upcasters.upcast(ev))
            return out

    async def load_all(
        self,
        from_global_position: int = 0,
        event_types: list[str] | None = None,
        batch_size: int = 500,
    ) -> AsyncIterator[StoredEvent]:
        last = from_global_position
        while True:
            where = "global_position > %s"
            params: list[Any] = [last]
            if event_types:
                where += " AND event_type = ANY(%s)"
                params.append(event_types)

            query = (
                "SELECT event_id, stream_id, stream_position, global_position, event_type, event_version, payload, metadata, recorded_at "
                f"FROM events WHERE {where} "
                "ORDER BY global_position ASC "
                "LIMIT %s"
            )
            params.append(batch_size)

            async with await psycopg.AsyncConnection.connect(self._dsn, row_factory=dict_row) as conn:
                cur = await conn.execute(query, params)
                batch: list[StoredEvent] = []
                async for r in cur:
                    ev = self._upcasters.upcast(StoredEvent(**r))
                    batch.append(ev)
                if not batch:
                    return
                for ev in batch:
                    last = ev.global_position
                    yield ev

    async def stream_version(self, stream_id: str) -> int:
        async with await psycopg.AsyncConnection.connect(self._dsn, row_factory=dict_row) as conn:
            row = await conn.execute(
                "SELECT current_version FROM event_streams WHERE stream_id = %s",
                (stream_id,),
            )
            r = await row.fetchone()
            return int(r["current_version"]) if r else 0

    async def archive_stream(self, stream_id: str) -> None:
        async with await psycopg.AsyncConnection.connect(self._dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE event_streams SET archived_at = NOW() WHERE stream_id = %s",
                    (stream_id,),
                )

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata:
        async with await psycopg.AsyncConnection.connect(self._dsn, row_factory=dict_row) as conn:
            row = await conn.execute(
                "SELECT stream_id, aggregate_type, current_version, created_at, archived_at, metadata "
                "FROM event_streams WHERE stream_id = %s",
                (stream_id,),
            )
            r = await row.fetchone()
            if not r:
                raise KeyError(f"stream not found: {stream_id}")
            return StreamMetadata(**r)

