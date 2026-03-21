from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.event_store import EventStore
from src.models.events import (
    ApplicationSubmitted,
    CreditAnalysisCompleted,
    OptimisticConcurrencyError,
)


@pytest.mark.asyncio
async def test_double_decision_concurrency(dsn: str) -> None:
    store = EventStore(dsn=dsn)
    stream_id = "loan-app-123"

    # Seed stream to version 3
    submitted = ApplicationSubmitted(
        application_id="app-123",
        applicant_id="user-1",
        requested_amount_usd=100_000.0,
        loan_purpose="working-capital",
        submission_channel="api",
        submitted_at=datetime.now(tz=timezone.utc),
    )
    v = await store.append(stream_id, [submitted, submitted, submitted], expected_version=-1, aggregate_type="LoanApplication")
    assert v == 3

    async def attempt(agent_id: str):
        ev = CreditAnalysisCompleted(
            application_id="app-123",
            agent_id=agent_id,
            session_id="s1",
            model_version="v2.3",
            confidence_score=0.9,
            risk_tier="MEDIUM",
            recommended_limit_usd=80_000.0,
            analysis_duration_ms=123,
            input_data_hash="hash",
        )
        return await store.append(stream_id, [ev], expected_version=3, aggregate_type="LoanApplication")

    results = await asyncio.gather(
        attempt("a1"),
        attempt("a2"),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, int)]
    failures = [r for r in results if isinstance(r, Exception)]

    assert len(successes) == 1
    assert successes[0] == 4

    assert len(failures) == 1
    assert isinstance(failures[0], OptimisticConcurrencyError)

    events = await store.load_stream(stream_id)
    assert len(events) == 4
    assert events[-1].stream_position == 4

