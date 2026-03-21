from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set (requires Postgres)")
    return url


@pytest.fixture(scope="session", autouse=True)
def apply_schema(dsn: str) -> None:
    schema_path = Path(__file__).resolve().parents[1] / "src" / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


@pytest.fixture(autouse=True)
def clean_db(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE "
                "outbox, events, event_streams, projection_checkpoints, "
                "application_summary, agent_performance_ledger, compliance_audit_events "
                "RESTART IDENTITY;"
            )
        conn.commit()

