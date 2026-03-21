from __future__ import annotations

from psycopg import AsyncConnection

from src.models.events import StoredEvent
from src.projections.base import Projection


class AgentPerformanceLedgerProjection(Projection):
    name = "AgentPerformanceLedger"
    subscribed_event_types = {
        "AgentContextLoaded",
        "CreditAnalysisCompleted",
        "DecisionGenerated",
        "HumanReviewCompleted",
    }

    async def handle(self, conn: AsyncConnection, event: StoredEvent) -> None:
        et = event.event_type
        p = event.payload

        agent_id = p.get("agent_id") or p.get("orchestrator_agent_id")
        model_version = p.get("model_version") or "unknown"
        if not agent_id:
            return

        await conn.execute(
            "INSERT INTO agent_performance_ledger (agent_id, model_version, first_seen_at, last_seen_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (agent_id, model_version) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at",
            (agent_id, model_version, event.recorded_at, event.recorded_at),
        )

        if et == "CreditAnalysisCompleted":
            conf = p.get("confidence_score")
            dur = p.get("analysis_duration_ms")
            await conn.execute(
                "UPDATE agent_performance_ledger SET "
                "analyses_completed = analyses_completed + 1, "
                "avg_confidence_score = CASE WHEN avg_confidence_score IS NULL THEN %s "
                "  ELSE (avg_confidence_score * analyses_completed + %s) / (analyses_completed + 1) END, "
                "avg_duration_ms = CASE WHEN avg_duration_ms IS NULL THEN %s "
                "  ELSE (avg_duration_ms * analyses_completed + %s) / (analyses_completed + 1) END "
                "WHERE agent_id=%s AND model_version=%s",
                (conf, conf, dur, dur, agent_id, model_version),
            )
        elif et == "DecisionGenerated":
            rec = p.get("recommendation")
            await conn.execute(
                "UPDATE agent_performance_ledger SET decisions_generated = decisions_generated + 1 WHERE agent_id=%s AND model_version=%s",
                (agent_id, model_version),
            )
            # naive rates
            if rec in {"APPROVE", "DECLINE", "REFER"}:
                await conn.execute(
                    "UPDATE agent_performance_ledger SET "
                    "approve_rate = CASE WHEN %s = 'APPROVE' THEN 1 ELSE 0 END, "
                    "decline_rate = CASE WHEN %s = 'DECLINE' THEN 1 ELSE 0 END, "
                    "refer_rate = CASE WHEN %s = 'REFER' THEN 1 ELSE 0 END "
                    "WHERE agent_id=%s AND model_version=%s",
                    (rec, rec, rec, agent_id, model_version),
                )

