# Sprint 1 — Understand the backend before extending it

## Objective

Make a deterministic support-operations API run against PostgreSQL, understand each layer,
and add one tested business capability without letting a coding agent do all the thinking.

## First session — Run and trace

1. Start PostgreSQL: `docker compose up -d db`.
2. Run migration `alembic upgrade head` and seed `python -m ai_operations_agent.seed`.
3. Run the API with `uvicorn ai_operations_agent.main:app --reload`.
4. Check `/health/ready`, `/api/v1/queues`, and `/api/v1/queues/1/metrics`.
5. Run `pytest -q` (quick SQLite path), then `TEST_DATABASE_URL="<your-dev-postgres-url>" pytest -q`.
6. Describe a complete ticket-creation request from `schemas.py` through `main.py` and `models.py` to PostgreSQL.

## First change — design it yourself

**Feature:** add `GET /api/v1/queues/{queue_id}/tickets-by-priority` returning
counts of active tickets by `low`, `normal`, and `high` priority.

Acceptance criteria:
- Only `open` and `in_progress` count.
- The response always contains all three keys, including zero counts.
- An unknown queue returns 404.
- Tests cover an empty queue, mixed priorities, resolved-ticket exclusion, and known counts.
- No extra dependency or raw SQL f-string; use SQLAlchemy parameterized queries.

**Before asking Codex to implement:** write down the expected JSON contract,
the SQL aggregation concept (`GROUP BY`), and three edge cases.

## Codex collaboration prompt

> Read AGENTS.md, README.md, docs/ARCHITECTURE.md and tests before proposing
> changes. Do not implement anything yet. Review the existing backend and show
> me the request lifecycle for POST /api/v1/tickets, the responsibilities of
> Pydantic, SQLAlchemy, Alembic, and the DB transaction, and three likely
> correctness risks. Ask me to explain my proposed design for the next endpoint
> before you generate code. Keep the scope strictly backend-only without LLMs.

## Definition of done

- PostgreSQL is healthy and initial migration + seed succeed on the Mac.
- API documentation loads at `/docs` and all documented endpoints respond.
- `pytest -q` and PostgreSQL integration tests pass.
- The feature above has test evidence, and migration/schema decisions are documented.
- You can explain the difference between an API validation check and a database constraint.

## Safety

This is a **local-only** learning backend, with example credentials and no user authentication.
Never deploy it to the public Internet unchanged. Do not run `docker compose down -v`
unless you intentionally want to delete all local database data.
