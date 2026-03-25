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


class DocumentUploadRequested(BaseEvent):
    event_type: Literal["DocumentUploadRequested"] = "DocumentUploadRequested"
    event_version: int = 1

    application_id: str
    document_type: str
    requested_at: datetime


class DocumentUploaded(BaseEvent):
    event_type: Literal["DocumentUploaded"] = "DocumentUploaded"
    event_version: int = 1

    application_id: str
    document_type: str
    file_path: str
    uploaded_at: datetime


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


class ComplianceRuleNoted(BaseEvent):
    event_type: Literal["ComplianceRuleNoted"] = "ComplianceRuleNoted"
    event_version: int = 1

    application_id: str
    rule_id: str
    rule_version: str
    note_type: str
    note: str
    evaluation_timestamp: datetime


class ComplianceCheckCompleted(BaseEvent):
    event_type: Literal["ComplianceCheckCompleted"] = "ComplianceCheckCompleted"
    event_version: int = 1

    application_id: str
    regulation_set_version: str
    overall_verdict: Literal["CLEAR", "BLOCKED", "CONDITIONAL"]
    has_hard_block: bool
    completed_at: datetime
    rules_evaluated: int


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


class HumanReviewOverride(BaseEvent):
    """Allows a subsequent CreditAnalysisCompleted (business rule 3 — analysis churn)."""

    event_type: Literal["HumanReviewOverride"] = "HumanReviewOverride"
    event_version: int = 1

    application_id: str
    reviewer_id: str
    reason: str


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


# ---- Support-doc AgentSession event catalogue (node-by-node audit trail) ----


class AgentSessionStarted(BaseEvent):
    event_type: Literal["AgentSessionStarted"] = "AgentSessionStarted"
    event_version: int = 1

    session_id: str
    agent_type: str
    model_version: str
    context_source: str
    context_token_count: int = 0


class AgentInputValidated(BaseEvent):
    event_type: Literal["AgentInputValidated"] = "AgentInputValidated"
    event_version: int = 1

    inputs_validated: list[str] = Field(default_factory=list)
    validation_duration_ms: int


class AgentInputValidationFailed(BaseEvent):
    event_type: Literal["AgentInputValidationFailed"] = "AgentInputValidationFailed"
    event_version: int = 1

    missing_inputs: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    validation_duration_ms: int


class AgentNodeExecuted(BaseEvent):
    event_type: Literal["AgentNodeExecuted"] = "AgentNodeExecuted"
    event_version: int = 1

    node_name: str
    node_sequence: int
    input_keys: list[str] = Field(default_factory=list)
    output_keys: list[str] = Field(default_factory=list)
    llm_called: bool = False
    llm_tokens_input: int | None = None
    llm_tokens_output: int | None = None
    llm_cost_usd: float | None = None
    duration_ms: int
    # Gas Town recovery: OK | PENDING | ERROR — PENDING/ERROR must appear verbatim in reconstructed context.
    execution_status: str = Field(default="OK")


class AgentToolCalled(BaseEvent):
    event_type: Literal["AgentToolCalled"] = "AgentToolCalled"
    event_version: int = 1

    tool_name: str
    tool_input_summary: str
    tool_output_summary: str
    tool_duration_ms: int


class AgentOutputWritten(BaseEvent):
    event_type: Literal["AgentOutputWritten"] = "AgentOutputWritten"
    event_version: int = 1

    events_written: list[dict[str, Any]] = Field(default_factory=list)
    output_summary: str


class AgentSessionCompleted(BaseEvent):
    event_type: Literal["AgentSessionCompleted"] = "AgentSessionCompleted"
    event_version: int = 1

    total_nodes_executed: int
    total_llm_calls: int
    total_tokens_used: int
    total_cost_usd: float
    next_agent_triggered: str | None = None


class AgentSessionFailed(BaseEvent):
    event_type: Literal["AgentSessionFailed"] = "AgentSessionFailed"
    event_version: int = 1

    error_type: str
    error_message: str
    last_successful_node: str | None = None
    recoverable: bool = False


class AgentSessionRecovered(BaseEvent):
    event_type: Literal["AgentSessionRecovered"] = "AgentSessionRecovered"
    event_version: int = 1

    recovered_from_session_id: str
    recovery_point: str


# ---- Support-doc DocumentPackage (docpkg-*) event catalogue (minimal) ----


class PackageCreated(BaseEvent):
    event_type: Literal["PackageCreated"] = "PackageCreated"
    event_version: int = 1

    package_id: str
    application_id: str
    created_at: datetime


class DocumentAdded(BaseEvent):
    event_type: Literal["DocumentAdded"] = "DocumentAdded"
    event_version: int = 1

    package_id: str
    document_type: str
    file_path: str


class DocumentFormatValidated(BaseEvent):
    event_type: Literal["DocumentFormatValidated"] = "DocumentFormatValidated"
    event_version: int = 1

    package_id: str
    document_type: str
    is_valid: bool
    notes: str | None = None


class ExtractionStarted(BaseEvent):
    event_type: Literal["ExtractionStarted"] = "ExtractionStarted"
    event_version: int = 1

    package_id: str
    document_type: str
    started_at: datetime


class ExtractionCompleted(BaseEvent):
    event_type: Literal["ExtractionCompleted"] = "ExtractionCompleted"
    event_version: int = 1

    package_id: str
    document_type: str
    completed_at: datetime
    extracted_facts: dict[str, Any] = Field(default_factory=dict)


class QualityAssessmentCompleted(BaseEvent):
    event_type: Literal["QualityAssessmentCompleted"] = "QualityAssessmentCompleted"
    event_version: int = 1

    package_id: str
    overall_confidence: float
    is_coherent: bool
    anomalies: list[str] = Field(default_factory=list)
    critical_missing_fields: list[str] = Field(default_factory=list)
    reextraction_recommended: bool = False
    auditor_notes: str = ""


class PackageReadyForAnalysis(BaseEvent):
    event_type: Literal["PackageReadyForAnalysis"] = "PackageReadyForAnalysis"
    event_version: int = 1

    package_id: str
    ready_at: datetime


EVENT_MODELS: dict[str, type[BaseEvent]] = {
    "ApplicationSubmitted": ApplicationSubmitted,
    "DocumentUploadRequested": DocumentUploadRequested,
    "DocumentUploaded": DocumentUploaded,
    "CreditAnalysisRequested": CreditAnalysisRequested,
    "CreditAnalysisCompleted": CreditAnalysisCompleted,
    "FraudScreeningCompleted": FraudScreeningCompleted,
    "ComplianceCheckRequested": ComplianceCheckRequested,
    "ComplianceRulePassed": ComplianceRulePassed,
    "ComplianceRuleFailed": ComplianceRuleFailed,
    "ComplianceRuleNoted": ComplianceRuleNoted,
    "ComplianceCheckCompleted": ComplianceCheckCompleted,
    "DecisionGenerated": DecisionGenerated,
    "HumanReviewCompleted": HumanReviewCompleted,
    "HumanReviewOverride": HumanReviewOverride,
    "ApplicationApproved": ApplicationApproved,
    "ApplicationDeclined": ApplicationDeclined,
    "AgentContextLoaded": AgentContextLoaded,
    "AgentSessionStarted": AgentSessionStarted,
    "AgentInputValidated": AgentInputValidated,
    "AgentInputValidationFailed": AgentInputValidationFailed,
    "AgentNodeExecuted": AgentNodeExecuted,
    "AgentToolCalled": AgentToolCalled,
    "AgentOutputWritten": AgentOutputWritten,
    "AgentSessionCompleted": AgentSessionCompleted,
    "AgentSessionFailed": AgentSessionFailed,
    "AgentSessionRecovered": AgentSessionRecovered,
    "PackageCreated": PackageCreated,
    "DocumentAdded": DocumentAdded,
    "DocumentFormatValidated": DocumentFormatValidated,
    "ExtractionStarted": ExtractionStarted,
    "ExtractionCompleted": ExtractionCompleted,
    "QualityAssessmentCompleted": QualityAssessmentCompleted,
    "PackageReadyForAnalysis": PackageReadyForAnalysis,
    "AuditIntegrityCheckRun": AuditIntegrityCheckRun,
}

