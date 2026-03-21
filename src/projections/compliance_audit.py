from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.models.events import StoredEvent
from src.projections.base import Projection


class ComplianceAuditViewProjection(Projection):
    name = "ComplianceAuditView"
    subscribed_event_types = {
        "ComplianceCheckRequested",
        "ComplianceRulePassed",
        "ComplianceRuleFailed",
    }

    async def handle(self, conn: AsyncConnection, event: StoredEvent) -> None:
        application_id = event.payload.get("application_id")
        if not application_id:
            return
        await conn.execute(
            "INSERT INTO compliance_audit_events (application_id, global_position, recorded_at, event_type, payload) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (application_id, global_position) DO NOTHING",
            (application_id, event.global_position, event.recorded_at, event.event_type, Jsonb(event.payload)),
        )


async def get_current_compliance(dsn: str, application_id: str) -> dict[str, Any]:
    async with await AsyncConnection.connect(dsn, row_factory=dict_row) as conn:
        cur = await conn.execute(
            "SELECT event_type, payload, recorded_at FROM compliance_audit_events "
            "WHERE application_id=%s ORDER BY recorded_at ASC",
            (application_id,),
        )
        events = [row async for row in cur]
    return _reduce(events)


async def get_compliance_at(dsn: str, application_id: str, as_of: datetime) -> dict[str, Any]:
    async with await AsyncConnection.connect(dsn, row_factory=dict_row) as conn:
        cur = await conn.execute(
            "SELECT event_type, payload, recorded_at FROM compliance_audit_events "
            "WHERE application_id=%s AND recorded_at <= %s ORDER BY recorded_at ASC",
            (application_id, as_of),
        )
        events = [row async for row in cur]
    return _reduce(events)


def _reduce(events: list[dict]) -> dict[str, Any]:
    state: dict[str, Any] = {"checks_required": [], "passed": [], "failed": []}
    for e in events:
        et = e["event_type"]
        p = e["payload"]
        if et == "ComplianceCheckRequested":
            state["regulation_set_version"] = p.get("regulation_set_version")
            state["checks_required"] = p.get("checks_required", [])
        elif et == "ComplianceRulePassed":
            state["passed"].append({"rule_id": p.get("rule_id"), "rule_version": p.get("rule_version")})
        elif et == "ComplianceRuleFailed":
            state["failed"].append({"rule_id": p.get("rule_id"), "rule_version": p.get("rule_version")})
    state["status"] = "FAILED" if state["failed"] else ("PASSED" if state["passed"] else "PENDING")
    return state

