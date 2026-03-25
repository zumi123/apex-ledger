# The Ledger (TRP1 Week 5)

PostgreSQL-backed, append-only event store with optimistic concurrency, projections (CQRS), upcasting, and audit integrity primitives.

## Prereqs

- Python 3.13+
- [`uv`](https://github.com/astral-sh/uv)
- A PostgreSQL 16+ instance (local install, or Docker)

## Quickstart

Install deps:

```bash
uv sync
```

Start Postgres (Docker, no compose):

```bash
docker run --rm --name ledger-postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_DB=ledger \
  -p 5432:5432 \
  postgres:16
```

In another terminal, apply schema:

```bash
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/ledger"
psql "$DATABASE_URL" -f src/schema.sql
```

Apply the Applicant Registry schema (support-document extension; read-only boundary):

```bash
psql "$DATABASE_URL" -f src/registry/schema.sql
```

Run tests:

```bash
uv run pytest -q
```

## Minimal demo (support-document overlay)

This seeds a tiny Applicant Registry dataset, creates dummy document files, seeds `ApplicationSubmitted`, then runs the stub `DocumentProcessingAgent`.

```bash
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/ledger"
uv run python datagen/minimal_generate.py --companies 1 --applications 1
uv run python scripts/run_doc_agent.py --app app-1 --company co-1
uv run python scripts/run_credit_agent.py --app app-1 --company co-1
uv run python scripts/run_fraud_agent.py --app app-1 --company co-1
uv run python scripts/run_compliance_agent.py --app app-1 --company co-1
uv run python scripts/run_orchestrator_agent.py --app app-1
 uv run python scripts/run_human_review.py --app app-1 --reviewer loan-officer-1 --final APPROVE
uv run python scripts/run_projections_once.py --app app-1
 uv run python scripts/show_application.py --app app-1
```

## One-command pipeline (end-to-end)

This runs: document processing → credit → fraud → compliance → orchestrator → human review → projections.

```bash
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/ledger"
uv run python datagen/minimal_generate.py --companies 1 --applications 1
uv run python scripts/run_pipeline.py --app app-1 --company co-1 --final APPROVE --reviewer loan-officer-1
uv run python scripts/show_application.py --app app-1
uv run python scripts/export_audit_report.py --app app-1 --out audit/app-1.json
```

## Web UI

Start the UI:

```bash
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/ledger"
uv sync
uv run python scripts/run_ui.py
```

Then open `http://127.0.0.1:8000` and click into an application. You can also record Human Review from the UI.

## Configuration

Tests and runtime use `DATABASE_URL` (e.g. `postgresql://user:pass@host:5432/dbname`).

