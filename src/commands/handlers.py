from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from src.aggregates.agent_session import AgentSessionAggregate
from src.aggregates.loan_application import ApplicationState, LoanApplicationAggregate
from src.event_store import EventStore
from src.models.events import (
    AgentContextLoaded,
    ApplicationSubmitted,
    CreditAnalysisCompleted,
    CreditAnalysisRequested,
)


class SubmitApplicationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    applicant_id: str
    requested_amount_usd: float
    loan_purpose: str
    submission_channel: str = "api"
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    correlation_id: str | None = None
    causation_id: str | None = None


class StartAgentSessionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    session_id: str
    context_source: str
    event_replay_from_position: int = 0
    context_token_count: int = 0
    model_version: str
    correlation_id: str | None = None
    causation_id: str | None = None


class CreditAnalysisCompletedCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: float | None = None
    risk_tier: str
    recommended_limit_usd: float
    duration_ms: int
    input_data_hash: str
    correlation_id: str | None = None
    causation_id: str | None = None


async def handle_submit_application(cmd: SubmitApplicationCommand, store: EventStore) -> int:
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    app.assert_new()

    ev = ApplicationSubmitted(
        application_id=cmd.application_id,
        applicant_id=cmd.applicant_id,
        requested_amount_usd=cmd.requested_amount_usd,
        loan_purpose=cmd.loan_purpose,
        submission_channel=cmd.submission_channel,
        submitted_at=cmd.submitted_at,
    )
    stream_id = f"loan-{cmd.application_id}"
    return await store.append(
        stream_id=stream_id,
        events=[ev],
        expected_version=-1,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
        aggregate_type="LoanApplication",
    )


async def handle_start_agent_session(cmd: StartAgentSessionCommand, store: EventStore) -> int:
    ev = AgentContextLoaded(
        agent_id=cmd.agent_id,
        session_id=cmd.session_id,
        context_source=cmd.context_source,
        event_replay_from_position=cmd.event_replay_from_position,
        context_token_count=cmd.context_token_count,
        model_version=cmd.model_version,
    )
    stream_id = f"agent-{cmd.agent_id}-{cmd.session_id}"
    return await store.append(
        stream_id=stream_id,
        events=[ev],
        expected_version=-1,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
        aggregate_type="AgentSession",
    )


async def handle_credit_analysis_completed(cmd: CreditAnalysisCompletedCommand, store: EventStore) -> int:
    # 1. Load current state
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    agent = await AgentSessionAggregate.load(store, cmd.agent_id, cmd.session_id)

    # 2. Validate
    app.assert_awaiting_credit_analysis()
    agent.assert_context_loaded()
    agent.assert_model_version_current(cmd.model_version)

    # 3. Determine new events
    new_events = [
        CreditAnalysisCompleted(
            application_id=cmd.application_id,
            agent_id=cmd.agent_id,
            session_id=cmd.session_id,
            model_version=cmd.model_version,
            confidence_score=cmd.confidence_score,
            risk_tier=cmd.risk_tier,
            recommended_limit_usd=cmd.recommended_limit_usd,
            analysis_duration_ms=cmd.duration_ms,
            input_data_hash=cmd.input_data_hash,
        )
    ]

    # 4. Append atomically to loan stream (optimistic concurrency via app.version)
    return await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=new_events,
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
        aggregate_type="LoanApplication",
    )


async def handle_credit_analysis_requested(application_id: str, assigned_agent_id: str, store: EventStore) -> int:
    app = await LoanApplicationAggregate.load(store, application_id)
    if app.state is not None and app.state != ApplicationState.SUBMITTED:
        # Keep minimal for now: only allow request right after submit
        raise ValueError("Invalid state for CreditAnalysisRequested")

    ev = CreditAnalysisRequested(
        application_id=application_id,
        assigned_agent_id=assigned_agent_id,
        requested_at=datetime.now(tz=timezone.utc),
        priority=0,
    )
    return await store.append(
        stream_id=f"loan-{application_id}",
        events=[ev],
        expected_version=app.version,
        aggregate_type="LoanApplication",
    )

