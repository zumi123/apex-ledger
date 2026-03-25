from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any

import psycopg
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from psycopg.rows import dict_row

from src.event_store import EventStore
from src.integrity.audit_chain import run_integrity_check
from src.mcp.errors import ToolError
from src.mcp.server import LedgerMCP
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon


TEMPLATES = Jinja2Templates(directory=str((__import__("pathlib").Path(__file__).resolve().parent / "templates")))

def _pretty_json(value: Any) -> Markup:
    s = json.dumps(value, indent=2, sort_keys=True, default=str)
    return Markup(escape(s))

def _json_safe(value: Any) -> Any:
    # Convert datetimes/UUIDs/etc into JSON-friendly primitives (strings).
    return json.loads(json.dumps(value, default=str))


def create_app(*, dsn: str | None = None) -> FastAPI:
    app = FastAPI(title="The Ledger UI")
    dsn_value = dsn or os.environ.get("DATABASE_URL")
    if not dsn_value:
        raise RuntimeError("DATABASE_URL is required to run the UI")

    store = EventStore(dsn=dsn_value)
    mcp = LedgerMCP(store)
    daemon = ProjectionDaemon(
        store,
        projections=[ApplicationSummaryProjection(), AgentPerformanceLedgerProjection(), ComplianceAuditViewProjection()],
    )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        async with await psycopg.AsyncConnection.connect(dsn_value, row_factory=dict_row) as conn:
            cur = await conn.execute(
                "SELECT application_id, state, decision, human_reviewer_id, last_event_at "
                "FROM application_summary ORDER BY last_event_at DESC NULLS LAST LIMIT 50"
            )
            rows = await cur.fetchall()
        return TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={"request": request, "apps": [dict(r) for r in rows]},
        )

    @app.get("/applications/{application_id}", response_class=HTMLResponse)
    async def view_application(request: Request, application_id: str) -> HTMLResponse:
        summary = await mcp.read_resource(f"ledger://applications/{application_id}")
        if summary is None:
            raise HTTPException(status_code=404, detail="Application not found in projection (run projections).")
        compliance = await mcp.read_resource(f"ledger://applications/{application_id}/compliance")
        audit = await mcp.read_resource(f"ledger://applications/{application_id}/audit-trail")
        integrity = await run_integrity_check(store, "loan", application_id)
        summary_display = _json_safe(summary)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="application.html",
            context={
                "request": request,
                "app_id": application_id,
                "state": summary_display.get("state"),
                "summary_display": summary_display,
                "summary_json": _pretty_json(summary),
                "compliance_json": _pretty_json(compliance),
                "audit_json": _pretty_json(audit),
                "integrity_json": _pretty_json(asdict(integrity)),
            },
        )

    @app.post("/applications/{application_id}/review")
    async def record_review(
        application_id: str,
        reviewer_id: str = Form(...),
        final_decision: str = Form(...),
        override: bool = Form(False),
        override_reason: str | None = Form(None),
    ) -> RedirectResponse:
        try:
            await mcp.call_tool(
                "record_human_review",
                {
                    "application_id": application_id,
                    "reviewer_id": reviewer_id,
                    "override": bool(override),
                    "final_decision": final_decision,
                    "override_reason": override_reason,
                },
            )
        except ToolError as e:
            raise HTTPException(status_code=400, detail=e.to_dict()) from e
        return RedirectResponse(url=f"/applications/{application_id}", status_code=303)

    @app.post("/applications/{application_id}/projections/run")
    async def run_projections(application_id: str) -> RedirectResponse:
        await daemon.run_once(batch_size=5000)
        return RedirectResponse(url=f"/applications/{application_id}", status_code=303)

    @app.get("/api/applications/{application_id}")
    async def api_application(application_id: str) -> dict[str, Any]:
        summary = await mcp.read_resource(f"ledger://applications/{application_id}")
        if summary is None:
            raise HTTPException(status_code=404, detail="Not found")
        return summary

    return app

