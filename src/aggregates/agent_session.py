from __future__ import annotations

from dataclasses import dataclass

from src.event_store import EventStore
from src.models.events import DomainError, StoredEvent


@dataclass
class AgentSessionAggregate:
    agent_id: str
    session_id: str
    version: int = 0
    context_loaded: bool = False
    model_version: str | None = None

    @classmethod
    async def load(cls, store: EventStore, agent_id: str, session_id: str) -> "AgentSessionAggregate":
        stream_id = f"agent-{agent_id}-{session_id}"
        events = await store.load_stream(stream_id)
        agg = cls(agent_id=agent_id, session_id=session_id)
        for ev in events:
            agg._apply(ev)
        return agg

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position

    def assert_context_loaded(self) -> None:
        if not self.context_loaded:
            raise DomainError("Agent session has no AgentContextLoaded event")

    def assert_model_version_current(self, model_version: str) -> None:
        if self.model_version is not None and self.model_version != model_version:
            raise DomainError(f"Model version mismatch: expected {self.model_version}, got {model_version}")

    def _on_AgentContextLoaded(self, event: StoredEvent) -> None:
        self.context_loaded = True
        self.model_version = event.payload.get("model_version")

