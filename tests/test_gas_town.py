from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.event_store import EventStore
from src.integrity.gas_town import reconstruct_agent_context
from src.models.events import AgentContextLoaded, CreditAnalysisCompleted, CreditAnalysisRequested


@pytest.mark.asyncio
async def test_reconstruct_agent_context_after_crash_completed_tail_ok(dsn: str) -> None:
    """Cold replay after 5 events ending in completed work: no pending items, healthy."""
    store = EventStore(dsn=dsn)
    agent_id = "agent-1"
    session_id = "sess-ok"
    stream_id = f"agent-{agent_id}-{session_id}"

    ev0 = AgentContextLoaded(
        agent_id=agent_id,
        session_id=session_id,
        context_source="replay",
        event_replay_from_position=0,
        context_token_count=123,
        model_version="v2.3",
    )
    await store.append(stream_id, [ev0], expected_version=-1, aggregate_type="AgentSession")

    for i in range(4):
        ev = CreditAnalysisCompleted(
            application_id=f"app-{i}",
            agent_id=agent_id,
            session_id=session_id,
            model_version="v2.3",
            confidence_score=0.8,
            risk_tier="LOW",
            recommended_limit_usd=1000.0,
            analysis_duration_ms=10,
            input_data_hash="h",
        )
        await store.append(stream_id, [ev], expected_version=i + 1, aggregate_type="AgentSession")

    ctx = await reconstruct_agent_context(store, agent_id, session_id, token_budget=200)
    assert ctx.last_event_position == 5
    assert ctx.pending_work == []
    assert ctx.session_health_status == "OK"
    assert "recent events (verbatim)" in ctx.context_text


@pytest.mark.asyncio
async def test_crash_recovery_pending_request_non_empty(dsn: str) -> None:
    """Simulate crash: last event is a request (no completion yet). pending_work must be non-empty."""
    store = EventStore(dsn=dsn)
    agent_id = "agent-crash"
    session_id = "sess-pending"
    stream_id = f"agent-{agent_id}-{session_id}"
    now = datetime.now(tz=timezone.utc)

    ev0 = AgentContextLoaded(
        agent_id=agent_id,
        session_id=session_id,
        context_source="replay",
        event_replay_from_position=0,
        context_token_count=50,
        model_version="v2.3",
    )
    await store.append(stream_id, [ev0], expected_version=-1, aggregate_type="AgentSession")

    for i in range(3):
        ev = CreditAnalysisCompleted(
            application_id=f"app-{i}",
            agent_id=agent_id,
            session_id=session_id,
            model_version="v2.3",
            confidence_score=0.8,
            risk_tier="LOW",
            recommended_limit_usd=1000.0,
            analysis_duration_ms=10,
            input_data_hash="h",
        )
        await store.append(stream_id, [ev], expected_version=i + 1, aggregate_type="AgentSession")

    # Fifth event: request emitted, process crashed before completion — recovery must surface this.
    req = CreditAnalysisRequested(
        application_id="app-pending",
        assigned_agent_id=agent_id,
        requested_at=now,
        priority=0,
    )
    await store.append(stream_id, [req], expected_version=4, aggregate_type="AgentSession")

    ctx = await reconstruct_agent_context(store, agent_id, session_id, token_budget=200)
    assert ctx.last_event_position == 5
    assert len(ctx.pending_work) >= 1
    assert any("CreditAnalysisRequested" in p for p in ctx.pending_work)
    assert ctx.session_health_status == "NEEDS_RECONCILIATION"
    assert "recent events (verbatim)" in ctx.context_text
    assert "app-pending" in ctx.context_text
