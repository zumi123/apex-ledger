"""Projection retries, compliance rebuild-from-scratch, and SLO-style catch-up bounds."""

from __future__ import annotations

import asyncio
import math
import os
import time
from datetime import datetime, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from src.event_store import EventStore
from src.mcp.resources import get_application_summary
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


@pytest.mark.asyncio
async def test_fifty_concurrent_submits_application_summary_lag_and_slo_time(dsn: str) -> None:
    """Simulate 50 concurrent command handlers appending new applications; catch-up stays within SLO.

    Rubric reference: lag clears quickly (≈500ms on quiet Postgres). Override with PROJECTION_SLO_CATCHUP_MS
    when CI runners are slower (default 1500ms).
    """
    store = EventStore(dsn=dsn)
    n = 50

    async def submit(i: int) -> None:
        app_id = f"app-hc-{i}"
        now = datetime.now(tz=timezone.utc)
        await store.append(
            stream_id=f"loan-{app_id}",
            events=[
                ApplicationSubmitted(
                    application_id=app_id,
                    applicant_id=f"u-{i}",
                    requested_amount_usd=1000.0,
                    loan_purpose="capex",
                    submission_channel="hc",
                    submitted_at=now,
                )
            ],
            expected_version=-1,
            aggregate_type="LoanApplication",
        )

    await asyncio.gather(*(submit(i) for i in range(n)))

    daemon = ProjectionDaemon(
        store,
        [
            ApplicationSummaryProjection(),
            AgentPerformanceLedgerProjection(),
            ComplianceAuditViewProjection(),
        ],
        retry_settings=ProjectionRetrySettings(initial_delay_sec=0.001, jitter_ratio=0.0),
    )

    slo_ms = float(os.environ.get("PROJECTION_SLO_CATCHUP_MS", "1500"))
    name = ApplicationSummaryProjection.name
    t0 = time.perf_counter()
    batches = 0
    while True:
        lag = await daemon.get_lag(name)
        if lag.lag_events == 0:
            break
        await daemon.run_once(batch_size=200)
        batches += 1
        assert batches <= 200, "SLO: catch-up exceeded batch budget"
    catch_up_ms = (time.perf_counter() - t0) * 1000.0

    assert catch_up_ms <= slo_ms, (
        f"ApplicationSummary catch-up took {catch_up_ms:.1f}ms (SLO {slo_ms}ms). "
        "Set PROJECTION_SLO_CATCHUP_MS higher on slow CI; rubric target ~500ms locally."
    )

    lag_final = await daemon.get_lag(name)
    assert lag_final.lag_events == 0
    row = await get_application_summary(dsn, "app-hc-0")
    assert row is not None
    assert row["state"] == "Submitted"


@pytest.mark.asyncio
async def test_rebuild_compliance_concurrent_reads_non_blocking(dsn: str) -> None:
    """rebuild_from_scratch runs while readers poll compliance + application_summary (no deadlock)."""
    store = EventStore(dsn=dsn)
    app_id = "app-rebuild-conc-1"
    n_rules = 8
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

    stop = asyncio.Event()

    async def reader_loop() -> int:
        n_reads = 0
        while not stop.is_set():
            await get_current_compliance(dsn, app_id)
            await get_application_summary(dsn, app_id)
            n_reads += 1
            await asyncio.sleep(0)
        return n_reads

    read_task = asyncio.create_task(reader_loop())
    try:
        stats = await asyncio.wait_for(
            daemon.rebuild_compliance_audit_view_from_scratch(batch_size=15, max_batches=100),
            timeout=60.0,
        )
    finally:
        stop.set()
        n_reads = await read_task

    assert n_reads >= 1
    assert stats["final_lag_events"] == 0
    after = await get_current_compliance(dsn, app_id)
    assert after == before
