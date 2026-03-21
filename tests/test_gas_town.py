from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.event_store import EventStore
from src.integrity.gas_town import reconstruct_agent_context
from src.models.events import AgentContextLoaded, CreditAnalysisCompleted


@pytest.mark.asyncio
async def test_reconstruct_agent_context_after_crash(dsn: str) -> None:
    store = EventStore(dsn=dsn)
    agent_id = "agent-1"
    session_id = "sess-1"
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

    # Append 4 more events
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
    assert "recent events" in ctx.context_text
    assert ctx.session_health_status in ("OK", "NEEDS_RECONCILIATION")

