from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OptimisticConcurrencyError(Exception):
    def __init__(
        self,
        *,
        stream_id: str,
        expected_version: int,
        actual_version: int,
        message: str | None = None,
    ) -> None:
        super().__init__(message or "Optimistic concurrency check failed")
        self.stream_id = stream_id
        self.expected_version = expected_version
        self.actual_version = actual_version


class DomainError(Exception):
    pass


class PreconditionFailed(Exception):
    pass


class BaseEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(..., description="Logical event name")
    event_version: int = Field(1, ge=1)

    def to_payload(self) -> dict[str, Any]:
        # Ensure JSON-serializable values (e.g. datetimes -> ISO strings)
        data = self.model_dump(mode="json")
        data.pop("event_type", None)
        data.pop("event_version", None)
        return data


class StoredEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    stream_id: str
    stream_position: int
    global_position: int
    event_type: str
    event_version: int
    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime

    def with_payload(self, payload: dict[str, Any], *, version: int) -> "StoredEvent":
        return self.model_copy(
            update={
                "payload": payload,
                "event_version": version,
            }
        )


class StreamMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime
    archived_at: datetime | None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---- Event catalogue (Pydantic payload validation) ----


class ApplicationSubmitted(BaseEvent):
    event_type: Literal["ApplicationSubmitted"] = "ApplicationSubmitted"
    event_version: int = 1

    application_id: str
    applicant_id: str
    requested_amount_usd: float
    loan_purpose: str
    submission_channel: str
    submitted_at: datetime


class CreditAnalysisRequested(BaseEvent):
    event_type: Literal["CreditAnalysisRequested"] = "CreditAnalysisRequested"
    event_version: int = 1

    application_id: str
    assigned_agent_id: str
    requested_at: datetime
    priority: int


class CreditAnalysisCompleted(BaseEvent):
    event_type: Literal["CreditAnalysisCompleted"] = "CreditAnalysisCompleted"
    event_version: int = 2

    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: float | None = None
    risk_tier: str
    recommended_limit_usd: float
    analysis_duration_ms: int
    input_data_hash: str


class FraudScreeningCompleted(BaseEvent):
    event_type: Literal["FraudScreeningCompleted"] = "FraudScreeningCompleted"
    event_version: int = 1

    application_id: str
    agent_id: str
    fraud_score: float
    anomaly_flags: list[str] = Field(default_factory=list)
    screening_model_version: str
    input_data_hash: str


class ComplianceCheckRequested(BaseEvent):
    event_type: Literal["ComplianceCheckRequested"] = "ComplianceCheckRequested"
    event_version: int = 1

    application_id: str
    regulation_set_version: str
    checks_required: list[str]


class ComplianceRulePassed(BaseEvent):
    event_type: Literal["ComplianceRulePassed"] = "ComplianceRulePassed"
    event_version: int = 1

    application_id: str
    rule_id: str
    rule_version: str
    evaluation_timestamp: datetime
    evidence_hash: str


class ComplianceRuleFailed(BaseEvent):
    event_type: Literal["ComplianceRuleFailed"] = "ComplianceRuleFailed"
    event_version: int = 1

    application_id: str
    rule_id: str
    rule_version: str
    failure_reason: str
    remediation_required: bool


class DecisionGenerated(BaseEvent):
    event_type: Literal["DecisionGenerated"] = "DecisionGenerated"
    event_version: int = 2

    application_id: str
    orchestrator_agent_id: str
    recommendation: Literal["APPROVE", "DECLINE", "REFER"]
    confidence_score: float
    contributing_agent_sessions: list[str]
    decision_basis_summary: str
    model_versions: dict[str, str] = Field(default_factory=dict)


class HumanReviewCompleted(BaseEvent):
    event_type: Literal["HumanReviewCompleted"] = "HumanReviewCompleted"
    event_version: int = 1

    application_id: str
    reviewer_id: str
    override: bool
    final_decision: Literal["APPROVE", "DECLINE", "REFER"]
    override_reason: str | None = None


class ApplicationApproved(BaseEvent):
    event_type: Literal["ApplicationApproved"] = "ApplicationApproved"
    event_version: int = 1

    application_id: str
    approved_amount_usd: float
    interest_rate: float
    conditions: list[str] = Field(default_factory=list)
    approved_by: str
    effective_date: datetime


class ApplicationDeclined(BaseEvent):
    event_type: Literal["ApplicationDeclined"] = "ApplicationDeclined"
    event_version: int = 1

    application_id: str
    decline_reasons: list[str]
    declined_by: str
    adverse_action_notice_required: bool


class AgentContextLoaded(BaseEvent):
    event_type: Literal["AgentContextLoaded"] = "AgentContextLoaded"
    event_version: int = 1

    agent_id: str
    session_id: str
    context_source: str
    event_replay_from_position: int
    context_token_count: int
    model_version: str


class AuditIntegrityCheckRun(BaseEvent):
    event_type: Literal["AuditIntegrityCheckRun"] = "AuditIntegrityCheckRun"
    event_version: int = 1

    entity_id: str
    check_timestamp: datetime
    events_verified_count: int
    integrity_hash: str
    previous_hash: str | None = None


EVENT_MODELS: dict[str, type[BaseEvent]] = {
    "ApplicationSubmitted": ApplicationSubmitted,
    "CreditAnalysisRequested": CreditAnalysisRequested,
    "CreditAnalysisCompleted": CreditAnalysisCompleted,
    "FraudScreeningCompleted": FraudScreeningCompleted,
    "ComplianceCheckRequested": ComplianceCheckRequested,
    "ComplianceRulePassed": ComplianceRulePassed,
    "ComplianceRuleFailed": ComplianceRuleFailed,
    "DecisionGenerated": DecisionGenerated,
    "HumanReviewCompleted": HumanReviewCompleted,
    "ApplicationApproved": ApplicationApproved,
    "ApplicationDeclined": ApplicationDeclined,
    "AgentContextLoaded": AgentContextLoaded,
    "AuditIntegrityCheckRun": AuditIntegrityCheckRun,
}

