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

    def expected_version_for_append(self) -> int:
        """Next append must use this value for optimistic concurrency (-1 = new stream)."""
        return -1 if self.version == 0 else self.version

    def assert_new_session(self) -> None:
        if self.version != 0:
            raise DomainError("Agent session stream already exists")

    def assert_context_loaded(self) -> None:
        if not self.context_loaded:
            raise DomainError("Agent session has no AgentContextLoaded event")

    def assert_model_version_current(self, model_version: str) -> None:
        if self.model_version is not None and self.model_version != model_version:
            raise DomainError(f"Model version mismatch: expected {self.model_version}, got {model_version}")

    def _on_AgentContextLoaded(self, event: StoredEvent) -> None:
        if self.context_loaded:
            raise DomainError("Invalid transition on AgentContextLoaded: context already loaded for this session")
        self.context_loaded = True
        self.model_version = event.payload.get("model_version")

