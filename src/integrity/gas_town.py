from __future__ import annotations

from dataclasses import dataclass

from src.event_store import EventStore
from src.models.events import StoredEvent


@dataclass(frozen=True)
class AgentContext:
    context_text: str
    last_event_position: int
    pending_work: list[str]
    session_health_status: str  # OK | NEEDS_RECONCILIATION


def _summarise_event(event_type: str, payload: dict) -> str:
    if event_type == "AgentContextLoaded":
        return f"Context loaded from {payload.get('context_source')} (model={payload.get('model_version')})."
    if "application_id" in payload:
        return f"{event_type} for application {payload.get('application_id')}."
    return f"{event_type}."


def _verbatim_line(e: StoredEvent) -> str:
    return f"{e.stream_position}: {_summarise_event(e.event_type, e.payload)}"


def _payload_status_pending_or_error(payload: dict) -> bool:
    for key in ("execution_status", "status", "state"):
        v = payload.get(key)
        if isinstance(v, str) and v.upper() in ("PENDING", "ERROR"):
            return True
    return False


def _should_preserve_verbatim(e: StoredEvent) -> bool:
    """Rubric: preserve any PENDING or ERROR state events in full (not only tail)."""
    if e.event_type == "AgentSessionFailed":
        return True
    if e.event_type.endswith("Requested"):
        return True
    return _payload_status_pending_or_error(e.payload)


def _pending_work_and_health(events: list[StoredEvent]) -> tuple[list[str], str]:
    """Outstanding tail work: request without downstream completion, failed session, or node stuck PENDING/ERROR."""
    if not events:
        return [], "OK"
    last = events[-1]
    pending: list[str] = []

    if last.event_type.endswith("Requested"):
        pending.append(f"reconcile:{last.event_type}")
        return pending, "NEEDS_RECONCILIATION"

    if last.event_type == "AgentSessionFailed":
        pending.append("recover:AgentSessionFailed")
        return pending, "NEEDS_RECONCILIATION"

    es = last.payload.get("execution_status")
    if isinstance(es, str) and es.upper() == "PENDING":
        node = last.payload.get("node_name", "?")
        pending.append(f"complete_node:{node}")
        return pending, "NEEDS_RECONCILIATION"

    if isinstance(es, str) and es.upper() == "ERROR":
        node = last.payload.get("node_name", "?")
        pending.append(f"retry_or_escalate_node:{node}")
        return pending, "NEEDS_RECONCILIATION"

    # Decision / output written but session never completed (crash before AgentSessionCompleted).
    if last.event_type == "AgentOutputWritten" and not any(e.event_type == "AgentSessionCompleted" for e in events):
        pending.append(f"await_session_completion:after:{last.event_type}")
        return pending, "NEEDS_RECONCILIATION"

    return [], "OK"


async def reconstruct_agent_context(
    store: EventStore,
    agent_id: str,
    session_id: str,
    token_budget: int = 8000,
) -> AgentContext:
    stream_id = f"agent-{agent_id}-{session_id}"
    events = await store.load_stream(stream_id)
    if not events:
        return AgentContext(context_text="", last_event_position=0, pending_work=[], session_health_status="OK")

    last = events[-1]
    last_pos = last.stream_position
    pending, health = _pending_work_and_health(events)

    preserved = events[-3:] if len(events) >= 3 else events[:]
    older = events[:-3] if len(events) > 3 else []

    lines: list[str] = []
    for e in older:
        if _should_preserve_verbatim(e):
            lines.append(_verbatim_line(e))
        else:
            lines.append(_summarise_event(e.event_type, e.payload))

    lines.append("---- recent events (verbatim) ----")
    for e in preserved:
        lines.append(_verbatim_line(e))

    text = "\n".join(lines)
    char_budget = max(4000, token_budget * 4)
    if len(text) > char_budget:
        text = text[-char_budget:]

    return AgentContext(
        context_text=text,
        last_event_position=last_pos,
        pending_work=pending,
        session_health_status=health,
    )
