from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from src.event_store import EventStore
from src.models.events import DomainError, StoredEvent


class ApplicationState(StrEnum):
    SUBMITTED = "Submitted"
    AWAITING_ANALYSIS = "AwaitingAnalysis"
    ANALYSIS_COMPLETE = "AnalysisComplete"
    COMPLIANCE_REVIEW = "ComplianceReview"
    PENDING_DECISION = "PendingDecision"
    APPROVED_PENDING_HUMAN = "ApprovedPendingHuman"
    DECLINED_PENDING_HUMAN = "DeclinedPendingHuman"
    FINAL_APPROVED = "FinalApproved"
    FINAL_DECLINED = "FinalDeclined"


@dataclass
class LoanApplicationAggregate:
    application_id: str
    version: int = 0
    state: ApplicationState | None = None

    applicant_id: str | None = None
    requested_amount_usd: float | None = None
    approved_amount_usd: float | None = None
    risk_tier: str | None = None
    fraud_score: float | None = None
    decision: str | None = None
    agent_sessions_completed: list[str] = field(default_factory=list)

    @classmethod
    async def load(cls, store: EventStore, application_id: str) -> "LoanApplicationAggregate":
        stream_id = f"loan-{application_id}"
        events = await store.load_stream(stream_id)
        agg = cls(application_id=application_id)
        for ev in events:
            agg._apply(ev)
        return agg

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position

    # ---- Invariants / guards ----

    def assert_new(self) -> None:
        if self.version != 0:
            raise DomainError("Application already exists")

    def assert_awaiting_credit_analysis(self) -> None:
        if self.state not in (ApplicationState.AWAITING_ANALYSIS,):
            raise DomainError(f"Expected AwaitingAnalysis, got {self.state}")

    # ---- Event handlers ----

    def _on_ApplicationSubmitted(self, event: StoredEvent) -> None:
        self.state = ApplicationState.SUBMITTED
        self.applicant_id = event.payload["applicant_id"]
        self.requested_amount_usd = float(event.payload["requested_amount_usd"])

    def _on_CreditAnalysisRequested(self, event: StoredEvent) -> None:
        self.state = ApplicationState.AWAITING_ANALYSIS

    def _on_CreditAnalysisCompleted(self, event: StoredEvent) -> None:
        self.state = ApplicationState.ANALYSIS_COMPLETE
        self.risk_tier = event.payload.get("risk_tier")

    def _on_DecisionGenerated(self, event: StoredEvent) -> None:
        self.state = ApplicationState.PENDING_DECISION
        self.decision = event.payload.get("recommendation")

    def _on_HumanReviewCompleted(self, event: StoredEvent) -> None:
        final = event.payload.get("final_decision")
        if final == "APPROVE":
            self.state = ApplicationState.FINAL_APPROVED
        elif final == "DECLINE":
            self.state = ApplicationState.FINAL_DECLINED
        else:
            self.state = ApplicationState.PENDING_DECISION

