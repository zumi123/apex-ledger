"""Projection retries, compliance rebuild-from-scratch, and SLO-style catch-up bounds."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from src.event_store import EventStore
from src.models.events import ApplicationSubmitted, ComplianceCheckRequested, ComplianceRulePassed
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.compliance_audit import (
    COMPLIANCE_AUDIT_PROJECTION_NAME,
    ComplianceAuditViewProjection,
    get_current_compliance,
)
from src.projections.daemon import (
    ProjectionDaemon,
    ProjectionRetrySettings,
    call_with_retries,
)


@pytest.mark.asyncio
async def test_call_with_retries_recoverable_operational_error() -> None:
    attempts = 0

    async def flaky() -> int:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise psycopg.OperationalError("simulated transient failure")
        return 42

    out = await call_with_retries(
        flaky,
        settings=ProjectionRetrySettings(
            max_attempts=5,
            initial_delay_sec=0.001,
            max_delay_sec=0.01,
            jitter_ratio=0.0,
        ),
    )
    assert out == 42
    assert attempts == 3


@pytest.mark.asyncio
async def test_call_with_retries_non_recoverable_raises_immediately() -> None:
    attempts = 0

    async def bad() -> None:
        nonlocal attempts
        attempts += 1
        raise ValueError("not retryable")

    with pytest.raises(ValueError, match="not retryable"):
        await call_with_retries(
            bad,
            settings=ProjectionRetrySettings(max_attempts=5, initial_delay_sec=0.001, jitter_ratio=0.0),
        )
    assert attempts == 1


@pytest.mark.asyncio
async def test_compliance_catch_up_slo_batch_bound(dsn: str) -> None:
    store = EventStore(dsn=dsn)
    app_id = "app-slo-1"
    n_rules = 48
    total_compliance_events = 1 + n_rules

    await _seed_async(store, app_id, n_rules)

    daemon = ProjectionDaemon(
        store,
        [
            ApplicationSummaryProjection(),
            AgentPerformanceLedgerProjection(),
            ComplianceAuditViewProjection(),
        ],
        retry_settings=ProjectionRetrySettings(initial_delay_sec=0.001, jitter_ratio=0.0),
    )

    batch_size = 12
    max_expected_batches = math.ceil(total_compliance_events / batch_size) + 3
    batches = 0
    while True:
        lag = await daemon.get_lag(COMPLIANCE_AUDIT_PROJECTION_NAME)
        if lag.lag_events == 0:
            break
        await daemon.run_once(batch_size=batch_size)
        batches += 1
        assert batches <= max_expected_batches, f"SLO: catch-up exceeded {max_expected_batches} batches"

    async with await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM compliance_audit_events WHERE application_id = %s",
            (app_id,),
        )
        row = await cur.fetchone()
        assert int(row["c"]) == total_compliance_events


async def _seed_async(store: EventStore, app_id: str, n_rules: int) -> None:
    now = datetime.now(tz=timezone.utc)
    loan = f"loan-{app_id}"
    comp = f"compliance-{app_id}"

    await store.append(
        stream_id=loan,
        events=[
            ApplicationSubmitted(
                application_id=app_id,
                applicant_id="u-slo",
                requested_amount_usd=25_000.0,
                loan_purpose="test",
                submission_channel="test",
                submitted_at=now,
            )
        ],
        expected_version=-1,
        aggregate_type="LoanApplication",
    )
    ver = await store.stream_version(comp)
    await store.append(
        stream_id=comp,
        events=[
            ComplianceCheckRequested(
                application_id=app_id,
                regulation_set_version="2026-Q1",
                checks_required=["REG-001"],
            )
        ],
        expected_version=-1 if ver == 0 else ver,
        aggregate_type="ComplianceRecord",
    )
    ver = await store.stream_version(comp)
    for i in range(n_rules):
        await store.append(
            stream_id=comp,
            events=[
                ComplianceRulePassed(
                    application_id=app_id,
                    rule_id="REG-001",
                    rule_version="2026-Q1",
                    evaluation_timestamp=now,
                    evidence_hash=f"h-{i}",
                )
            ],
            expected_version=ver,
            aggregate_type="ComplianceRecord",
        )
        ver = await store.stream_version(comp)


@pytest.mark.asyncio
async def test_rebuild_compliance_audit_view_from_scratch_restores_read_model(dsn: str) -> None:
    store = EventStore(dsn=dsn)
    app_id = "app-rebuild-1"
    n_rules = 5
    await _seed_async(store, app_id, n_rules)

    daemon = ProjectionDaemon(
        store,
        [
            ApplicationSummaryProjection(),
            AgentPerformanceLedgerProjection(),
            ComplianceAuditViewProjection(),
        ],
        retry_settings=ProjectionRetrySettings(jitter_ratio=0.0),
    )
    while (await daemon.get_lag(COMPLIANCE_AUDIT_PROJECTION_NAME)).lag_events > 0:
        await daemon.run_once(batch_size=50)

    before = await get_current_compliance(dsn, app_id)
    assert before["status"] == "PASSED"
    assert len(before["passed"]) == n_rules

    stats = await daemon.rebuild_compliance_audit_view_from_scratch(batch_size=20, max_batches=50)
    assert stats["final_lag_events"] == 0
    assert stats["batches"] >= 1

    after = await get_current_compliance(dsn, app_id)
    assert after == before

    lag = await daemon.get_lag(COMPLIANCE_AUDIT_PROJECTION_NAME)
    assert lag.lag_events == 0
