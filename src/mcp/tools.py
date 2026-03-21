from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.commands.handlers import (
    CreditAnalysisCompletedCommand,
    StartAgentSessionCommand,
    SubmitApplicationCommand,
    handle_credit_analysis_completed,
    handle_credit_analysis_requested,
    handle_start_agent_session,
    handle_submit_application,
)
from src.event_store import EventStore
from src.mcp.errors import ToolError
from src.models.events import OptimisticConcurrencyError


async def submit_application(store: EventStore, **params: Any) -> dict[str, Any]:
    try:
        cmd = SubmitApplicationCommand(**params)
        v = await handle_submit_application(cmd, store)
        return {"stream_id": f"loan-{cmd.application_id}", "initial_version": v}
    except Exception as e:  # noqa: BLE001
        raise ToolError("ValidationError", str(e), suggested_action="fix_parameters") from e


async def start_agent_session(store: EventStore, **params: Any) -> dict[str, Any]:
    try:
        cmd = StartAgentSessionCommand(**params)
        v = await handle_start_agent_session(cmd, store)
        return {"session_id": cmd.session_id, "context_position": v}
    except Exception as e:  # noqa: BLE001
        raise ToolError("ValidationError", str(e), suggested_action="fix_parameters") from e


async def record_credit_analysis(store: EventStore, **params: Any) -> dict[str, Any]:
    try:
        cmd = CreditAnalysisCompletedCommand(**params)
        v = await handle_credit_analysis_completed(cmd, store)
        return {"new_stream_version": v}
    except OptimisticConcurrencyError as e:
        raise ToolError(
            "OptimisticConcurrencyError",
            str(e),
            suggested_action="reload_stream_and_retry",
            stream_id=e.stream_id,
            expected_version=e.expected_version,
            actual_version=e.actual_version,
        ) from e
    except Exception as e:  # noqa: BLE001
        raise ToolError("DomainError", str(e), suggested_action="check_preconditions") from e


async def request_credit_analysis(store: EventStore, application_id: str, assigned_agent_id: str) -> dict[str, Any]:
    v = await handle_credit_analysis_requested(application_id, assigned_agent_id, store)
    return {"new_stream_version": v}

