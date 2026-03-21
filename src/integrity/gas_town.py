from __future__ import annotations

from dataclasses import dataclass

from src.event_store import EventStore


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

    # Heuristic reconciliation detection: last event looks like a "Requested" without a later "Completed".
    needs_recon = False
    if last.event_type.endswith("Requested"):
        needs_recon = True

    preserved = events[-3:] if len(events) >= 3 else events[:]
    older = events[:-3] if len(events) > 3 else []

    lines: list[str] = []
    for e in older:
        lines.append(_summarise_event(e.event_type, e.payload))

    lines.append("---- recent events ----")
    for e in preserved:
        lines.append(f"{e.stream_position}: {_summarise_event(e.event_type, e.payload)}")

    text = "\n".join(lines)
    if len(text) > token_budget * 4:
        text = text[-token_budget * 4 :]

    pending: list[str] = []
    if needs_recon:
        pending.append(f"reconcile:{last.event_type}")

    return AgentContext(
        context_text=text,
        last_event_position=last_pos,
        pending_work=pending,
        session_health_status="NEEDS_RECONCILIATION" if needs_recon else "OK",
    )

