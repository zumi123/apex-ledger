from __future__ import annotations

from src.upcasting.registry import DEFAULT_UPCASTERS


@DEFAULT_UPCASTERS.register("CreditAnalysisCompleted", from_version=1)
def upcast_credit_v1_to_v2(payload: dict) -> dict:
    # Inference strategy: model_version is not reliably recoverable for legacy events.
    # We keep a sentinel and avoid fabricating confidence_score.
    return {
        **payload,
        "model_version": payload.get("model_version", "legacy-pre-2026"),
        "confidence_score": payload.get("confidence_score"),
        "regulatory_basis": payload.get("regulatory_basis"),
    }


@DEFAULT_UPCASTERS.register("DecisionGenerated", from_version=1)
def upcast_decision_v1_to_v2(payload: dict) -> dict:
    return {
        **payload,
        "model_versions": payload.get("model_versions", {}),
    }

