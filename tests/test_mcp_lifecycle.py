from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.event_store import EventStore
from src.mcp.server import LedgerMCP


@pytest.mark.asyncio
async def test_mcp_basic_lifecycle(dsn: str) -> None:
    store = EventStore(dsn=dsn)
    mcp = LedgerMCP(store)

    app_id = "app-mcp-1"

    await mcp.call_tool(
        "submit_application",
        {
            "application_id": app_id,
            "applicant_id": "user-1",
            "requested_amount_usd": 100_000.0,
            "loan_purpose": "capex",
            "submission_channel": "mcp",
            "submitted_at": datetime.now(tz=timezone.utc),
        },
    )

    await mcp.call_tool(
        "request_credit_analysis",
        {"application_id": app_id, "assigned_agent_id": "agent-1"},
    )

    await mcp.call_tool(
        "start_agent_session",
        {
            "agent_id": "agent-1",
            "session_id": "s1",
            "context_source": "replay",
            "event_replay_from_position": 0,
            "context_token_count": 123,
            "model_version": "v2.3",
        },
    )

    await mcp.call_tool(
        "record_credit_analysis",
        {
            "application_id": app_id,
            "agent_id": "agent-1",
            "session_id": "s1",
            "model_version": "v2.3",
            "confidence_score": 0.9,
            "risk_tier": "MEDIUM",
            "recommended_limit_usd": 80_000.0,
            "duration_ms": 10,
            "input_data_hash": "h",
        },
    )

    # Process projections and query via resources.
    await mcp.daemon.run_once()

    summary = await mcp.read_resource(f"ledger://applications/{app_id}")
    assert summary is not None
    assert summary["application_id"] == app_id
    assert summary["state"] in ("Submitted", "AwaitingAnalysis", "AnalysisComplete")

