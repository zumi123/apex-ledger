from __future__ import annotations

from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from src.event_store import EventStore


async def test_upcasting_immutability(dsn: str) -> None:
    stream_id = "loan-upcast-1"
    # Insert a legacy v1 CreditAnalysisCompleted payload directly (no model_version/confidence_score).
    legacy_payload = {
        "application_id": "app-legacy",
        "agent_id": "a1",
        "session_id": "s1",
        "risk_tier": "MEDIUM",
        "recommended_limit_usd": 1000.0,
        "analysis_duration_ms": 12,
        "input_data_hash": "h",
    }

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO event_streams (stream_id, aggregate_type, current_version) VALUES (%s, %s, 1)",
                (stream_id, "LoanApplication"),
            )
            row = conn.execute(
                "INSERT INTO events (stream_id, stream_position, event_type, event_version, payload, metadata, recorded_at) "
                "VALUES (%s, 1, 'CreditAnalysisCompleted', 1, %s::jsonb, '{}'::jsonb, %s) "
                "RETURNING event_id",
                (stream_id, psycopg.types.json.Jsonb(legacy_payload), datetime.now(tz=timezone.utc)),
            ).fetchone()
            event_id = row["event_id"]
        conn.commit()

    # Load through store: should upcast to v2 without mutating DB.
    store = EventStore(dsn=dsn)
    events = await store.load_stream(stream_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_id == event_id
    assert ev.event_version == 2
    assert ev.payload["model_version"] == "legacy-pre-2026"
    assert "confidence_score" in ev.payload

    # Raw DB payload unchanged (still v1 shape).
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        raw = conn.execute("SELECT payload FROM events WHERE event_id = %s", (event_id,)).fetchone()["payload"]
        assert raw == legacy_payload

