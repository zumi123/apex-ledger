from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from psycopg import AsyncConnection

from src.models.events import StoredEvent


@dataclass(frozen=True)
class ProjectionLag:
    projection_name: str
    last_processed_global_position: int
    latest_global_position: int
    lag_events: int


class Projection:
    name: str
    subscribed_event_types: set[str]

    async def handle(self, conn: AsyncConnection, event: StoredEvent) -> None:  # pragma: no cover
        raise NotImplementedError

