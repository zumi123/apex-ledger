from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

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

    def expected_version_for_append(self) -> int:
        """Next append must use this value for optimistic concurrency (-1 = new stream)."""
        return -1 if self.version == 0 else self.version

    def _require_states(
        self,
        allowed: set[ApplicationState | None],
        event_type: str,
    ) -> None:
        if self.state not in allowed:
            raise DomainError(
                f"Invalid transition on {event_type}: current state is {self.state!r}, "
                f"allowed predecessor states: {sorted(s for s in allowed if s is not None)!r} "
                f"{'or empty stream' if None in allowed else ''}"
            )

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position

    # ---- Invariants / guards (command side) ----

    def assert_new(self) -> None:
        if self.version != 0:
            raise DomainError("Application already exists")

    def assert_allows_credit_analysis_requested(self) -> None:
        self._require_states({ApplicationState.SUBMITTED}, "CreditAnalysisRequested")

    def assert_awaiting_credit_analysis(self) -> None:
        self._require_states({ApplicationState.AWAITING_ANALYSIS}, "CreditAnalysisCompleted")

    # ---- Event handlers (replay): explicit valid transitions ----

    def _on_ApplicationSubmitted(self, event: StoredEvent) -> None:
        self._require_states({None}, "ApplicationSubmitted")
        self.state = ApplicationState.SUBMITTED
        self.applicant_id = event.payload["applicant_id"]
        self.requested_amount_usd = float(event.payload["requested_amount_usd"])

    def _on_CreditAnalysisRequested(self, event: StoredEvent) -> None:
        self._require_states({ApplicationState.SUBMITTED}, "CreditAnalysisRequested")
        self.state = ApplicationState.AWAITING_ANALYSIS

    def _on_CreditAnalysisCompleted(self, event: StoredEvent) -> None:
        self._require_states({ApplicationState.AWAITING_ANALYSIS}, "CreditAnalysisCompleted")
        self.state = ApplicationState.ANALYSIS_COMPLETE
        self.risk_tier = event.payload.get("risk_tier")

    def _on_ComplianceCheckRequested(self, event: StoredEvent) -> None:
        self._require_states({ApplicationState.ANALYSIS_COMPLETE}, "ComplianceCheckRequested")
        self.state = ApplicationState.COMPLIANCE_REVIEW

    def _on_ComplianceRulePassed(self, event: StoredEvent) -> None:
        self._require_states({ApplicationState.COMPLIANCE_REVIEW}, "ComplianceRulePassed")

    def _on_ComplianceRuleFailed(self, event: StoredEvent) -> None:
        self._require_states({ApplicationState.COMPLIANCE_REVIEW}, "ComplianceRuleFailed")

    def _on_DecisionGenerated(self, event: StoredEvent) -> None:
        self._require_states(
            {ApplicationState.COMPLIANCE_REVIEW, ApplicationState.ANALYSIS_COMPLETE},
            "DecisionGenerated",
        )
        self.state = ApplicationState.PENDING_DECISION
        self.decision = event.payload.get("recommendation")

    def _on_HumanReviewCompleted(self, event: StoredEvent) -> None:
        self._require_states({ApplicationState.PENDING_DECISION}, "HumanReviewCompleted")
        final = event.payload.get("final_decision")
        if final == "APPROVE":
            self.state = ApplicationState.FINAL_APPROVED
        elif final == "DECLINE":
            self.state = ApplicationState.FINAL_DECLINED
        else:
            self.state = ApplicationState.PENDING_DECISION

    def _on_ApplicationApproved(self, event: StoredEvent) -> None:
        self._require_states(
            {ApplicationState.APPROVED_PENDING_HUMAN},
            "ApplicationApproved",
        )
        self.state = ApplicationState.FINAL_APPROVED
        self.approved_amount_usd = float(event.payload["approved_amount_usd"])

    def _on_ApplicationDeclined(self, event: StoredEvent) -> None:
        self._require_states({ApplicationState.DECLINED_PENDING_HUMAN}, "ApplicationDeclined")
        self.state = ApplicationState.FINAL_DECLINED
