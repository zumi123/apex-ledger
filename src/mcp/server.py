from __future__ import annotations

from datetime import datetime
from typing import Any

from src.event_store import EventStore
from src.mcp.errors import ToolError
from src.mcp import resources, tools
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon


class LedgerMCP:
    """
    Minimal in-process MCP-like dispatcher.

    Tools write events (command side). Resources read from projections (query side).
    """

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.daemon = ProjectionDaemon(
            store,
            projections=[
                ApplicationSummaryProjection(),
                AgentPerformanceLedgerProjection(),
                ComplianceAuditViewProjection(),
            ],
        )

    async def call_tool(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "submit_application":
                return await tools.submit_application(self.store, **params)
            if name == "start_agent_session":
                return await tools.start_agent_session(self.store, **params)
            if name == "record_credit_analysis":
                return await tools.record_credit_analysis(self.store, **params)
            if name == "request_credit_analysis":
                return await tools.request_credit_analysis(self.store, **params)
            raise ToolError("UnknownTool", f"Unknown tool: {name}")
        except ToolError:
            raise

    async def read_resource(self, uri: str, *, as_of: datetime | None = None) -> Any:
        # Supported URIs (subset of the spec):
        # - ledger://applications/{id}
        # - ledger://applications/{id}/compliance
        # - ledger://applications/{id}/audit-trail
        # - ledger://ledger/health
        parts = uri.removeprefix("ledger://").split("/")
        if parts[0] == "applications" and len(parts) >= 2:
            app_id = parts[1]
            if len(parts) == 2:
                return await resources.get_application_summary(self.store._dsn, app_id)  # noqa: SLF001
            if len(parts) == 3 and parts[2] == "compliance":
                return await resources.get_compliance(self.store._dsn, app_id, as_of=as_of)  # noqa: SLF001
            if len(parts) == 3 and parts[2] == "audit-trail":
                return await resources.get_audit_trail(self.store, app_id)
        if parts[0] == "ledger" and len(parts) == 2 and parts[1] == "health":
            return await resources.get_health(self.daemon)
        raise ToolError("UnknownResource", f"Unknown resource: {uri}")

