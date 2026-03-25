from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

import psycopg
from psycopg.rows import dict_row

from src.event_store import EventStore
from src.models.events import StoredEvent
from src.projections.base import Projection, ProjectionLag
from src.projections.compliance_audit import COMPLIANCE_AUDIT_PROJECTION_NAME

T = TypeVar("T")


@dataclass(frozen=True)
class ProjectionHealth:
    name: str
    lag: ProjectionLag


@dataclass(frozen=True)
class ProjectionRetrySettings:
    """Transient DB / driver failures during projection.handle — bounded exponential backoff."""

    max_attempts: int = 5
    initial_delay_sec: float = 0.05
    max_delay_sec: float = 2.0
    exponential_base: float = 2.0
    jitter_ratio: float = 0.1  # 0 disables jitter


def _retryable_projection_error(exc: BaseException) -> bool:
    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        return True
    err = getattr(exc, "__cause__", None)
    if isinstance(err, (psycopg.OperationalError, psycopg.InterfaceError)):
        return True
    return False


async def _sleep_backoff(attempt: int, settings: ProjectionRetrySettings) -> None:
    delay = min(
        settings.max_delay_sec,
        settings.initial_delay_sec * (settings.exponential_base ** (attempt - 1)),
    )
    if settings.jitter_ratio > 0:
        jitter = delay * settings.jitter_ratio
        delay = max(0.0, delay + random.uniform(-jitter, jitter))
    await asyncio.sleep(delay)


async def call_with_retries(
    fn: Callable[[], Awaitable[T]],
    *,
    settings: ProjectionRetrySettings,
    is_retryable: Callable[[BaseException], bool] = _retryable_projection_error,
) -> T:
    last: BaseException | None = None
    for attempt in range(1, settings.max_attempts + 1):
        try:
            return await fn()
        except BaseException as e:
            last = e
            if attempt >= settings.max_attempts or not is_retryable(e):
                raise
            await _sleep_backoff(attempt, settings)
    assert last is not None
    raise last


class ProjectionDaemon:
    def __init__(
        self,
        store: EventStore,
        projections: list[Projection],
        *,
        retry_settings: ProjectionRetrySettings | None = None,
    ) -> None:
        self._store = store
        self._projections = {p.name: p for p in projections}
        self._running = False
        self._retry_settings = retry_settings or ProjectionRetrySettings()

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
                # If a projection does not subscribe to this event type, advance its checkpoint anyway
                # (no-op cursor) so min(checkpoints) moves forward and we do not re-read the same window forever.
                for ev in events:
                    for name, proj in self._projections.items():
                        if ev.event_type not in proj.subscribed_event_types:
                            if checkpoints[name] < ev.global_position:
                                checkpoints[name] = ev.global_position
                                await self._set_checkpoint(conn, name, checkpoints[name])
                            continue
                        if checkpoints[name] >= ev.global_position:
                            continue

                        p, e = proj, ev

                        async def _apply() -> None:
                            await p.handle(conn, e)

                        await call_with_retries(_apply, settings=self._retry_settings)
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

    async def rebuild_compliance_audit_view_from_scratch(
        self,
        *,
        batch_size: int = 500,
        max_batches: int | None = None,
    ) -> dict[str, int]:
        """Truncate compliance_audit_events, reset ComplianceAuditView checkpoint to 0, then catch up from the event log.

        Other projections are unchanged; shared batches skip events they have already processed.
        """
        if COMPLIANCE_AUDIT_PROJECTION_NAME not in self._projections:
            raise ValueError(
                f"ProjectionDaemon must include {COMPLIANCE_AUDIT_PROJECTION_NAME} for rebuild"
            )

        async with await psycopg.AsyncConnection.connect(self._store._dsn, row_factory=dict_row) as conn:  # noqa: SLF001
            await conn.execute("TRUNCATE compliance_audit_events")
            await conn.execute(
                "INSERT INTO projection_checkpoints (projection_name, last_position) VALUES (%s, 0) "
                "ON CONFLICT (projection_name) DO UPDATE SET last_position = 0, updated_at = NOW()",
                (COMPLIANCE_AUDIT_PROJECTION_NAME,),
            )
            await conn.commit()

        batches = 0
        while True:
            lag = await self.get_lag(COMPLIANCE_AUDIT_PROJECTION_NAME)
            if lag.lag_events == 0:
                return {"batches": batches, "final_lag_events": 0}
            await self.run_once(batch_size=batch_size)
            batches += 1
            if max_batches is not None and batches >= max_batches:
                lag2 = await self.get_lag(COMPLIANCE_AUDIT_PROJECTION_NAME)
                raise RuntimeError(
                    f"rebuild exceeded max_batches={max_batches}; lag_events={lag2.lag_events}"
                )

