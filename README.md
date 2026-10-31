# AI Operations Agent

A backend-first learning project: build the deterministic operations system **before**
introducing LLM agents. The first version models support queues, human operators,
and tickets, with a small metrics API and repeatable tests.

> **Status:** Sprint 1 starter. Local development only; no LLM, no authentication, no public deployment.

## Tech stack

- Python 3.11+ / FastAPI / Pydantic
- PostgreSQL 16 / SQLAlchemy 2.x / Alembic
- Pytest / Ruff / GitHub Actions

## Quick start (Mac)

Install Python 3.11+ and Docker Desktop. Ensure Docker Desktop is running.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
docker compose up -d db
docker compose ps  # wait until healthy
alembic upgrade head
python -m ai_operations_agent.seed
uvicorn ai_operations_agent.main:app --reload
```

Open http://127.0.0.1:8000/docs for Swagger UI, or try:

```bash
curl http://127.0.0.1:8000/health/ready
curl http://127.0.0.1:8000/api/v1/queues
curl http://127.0.0.1:8000/api/v1/queues/1/metrics
curl 'http://127.0.0.1:8000/api/v1/tickets?queue_id=1&status=open&limit=5'
```

Sample data: **3 queues, 12 agents, 90 tickets**. Seeding a second time is non-destructive
and prints a skip message. Keep development data and test data in their separate volumes.

Port 5433 is used on the Mac to avoid colliding with a PostgreSQL server on 5432.

`DATABASE_URL` is required, from the process environment or the local `.env` file.
Copying `.env.example` as shown above preserves the local development configuration.
Application settings and Alembic share this value; missing configuration fails at startup
before opening a database connection. Demonstration credentials belong only in the
explicitly local `.env.example` and `docker-compose.yml` examples.

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/health/live` | Process liveness (does not require DB) |
| GET | `/health/ready` | DB readiness |
| GET | `/api/v1/queues` | List support queues |
| GET | `/api/v1/agents?queue_id=&status=` | Filter agents |
| GET | `/api/v1/tickets?queue_id=&status=&priority=&limit=&offset=` | Filter / paginate tickets |
| POST | `/api/v1/tickets` | Create a support ticket |
| PATCH | `/api/v1/tickets/{id}/status` | Advance ticket status |
| GET | `/api/v1/queues/{id}/metrics` | Current queue backlog / SLA breach metric |
| GET | `/api/v1/queues/{queue_id}/tickets-by-priority` | Active ticket counts by priority |

Status transitions: `open -> in_progress -> resolved`, with `open -> resolved` also allowed.
Resolved tickets are terminal in this starter.

`GET /api/v1/queues/{queue_id}/tickets-by-priority` counts only `open` and
`in_progress` tickets in the requested queue, grouped by priority in SQL.
Resolved tickets are excluded. The response always includes nonnegative integer
counts for `low`, `normal`, and `high`, including zeros:

```json
{
  "queue_id": 1,
  "counts": {"low": 0, "normal": 1, "high": 1}
}
```

An existing queue with no active tickets returns HTTP 200 with all three counts
set to zero. A nonexistent queue returns HTTP 404 with `{"detail":"queue not found"}`;
a path ID that cannot be parsed as an integer returns HTTP 422.

Example ticket creation:

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/tickets \
  -H 'Content-Type: application/json' \
  -d '{"external_id":"DEMO-101","subject":"Login issue","queue_id":1,"priority":"high"}'
```

## Tests and quality checks

```bash
env -u TEST_DATABASE_URL .venv/bin/python -m pytest -q  # in-memory SQLite
.venv/bin/python -m ruff check .
git diff --check
```

Run the **same API and Feature 001 tests against migrated PostgreSQL 16**. The `db_test`
service binds only to `127.0.0.1:5434`, uses database `ai_operations_test`, and stores data
in `postgres_test_data`, independently of the development `db` service on port 5433.
From the repository root:

```bash
docker compose up -d --wait db_test
# Read only the explicitly local test URL from the tracked example, without sourcing .env.
export TEST_DATABASE_URL="$(sed -n 's/^TEST_DATABASE_URL=//p' .env.example)"
# DATABASE_URL is overridden only for this migration process; .env stays unchanged.
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/python -m alembic upgrade head
.venv/bin/python -m pytest -q
```

Never copy the development `DATABASE_URL` into `TEST_DATABASE_URL`. Before opening a
connection or creating fixtures, pytest requires a PostgreSQL URL with an explicit host,
a database name ending in `_test`, and a database name different from the configured
development database. Connection query parameters are rejected because they can override
the target. Empty or invalid values fail immediately; unset the variable to use SQLite.

PostgreSQL tests use an outer transaction and savepoints so API commits and rollbacks,
including status transitions, stay isolated and are rolled back after each test. Fixtures
create their own data; do not seed the test database. PostgreSQL sequences can advance
despite rollback, so tests do not depend on fixed IDs. Tests never create or drop PostgreSQL
tables; Alembic owns the schema. Run one test process at a time against this database.
CI generates fresh credentials for its own temporary PostgreSQL 16 container, with a
loopback-only dynamic port and memory-backed storage. It migrates only that test database,
runs the integration suite, and removes the container on completion, including failures.
CI does not reuse the local example credentials or development database configuration.

Return to normal SQLite tests and optionally stop only the test service:

```bash
unset TEST_DATABASE_URL
.venv/bin/python -m pytest -q
docker compose stop db_test
```

Stopping the test service preserves both volumes. Do not remove volumes to switch test modes.

## Learning plan

1. Explain schema ownership and migration history (`alembic/versions/0001_initial_schema.py`).
2. Trace a POST request from Pydantic validation to transaction commit.
3. Write a new failing test for a business rule before implementing the change.
4. Add an endpoint to show tickets by age bucket, with tests for all boundaries.
5. Only after the deterministic baseline is solid, add an LLM that can *recommend*
   actions, with mandatory server-side approval and checks.

See [architecture notes](docs/ARCHITECTURE.md) for invariants and limitations,
and [Sprint 1 learning exercises](docs/SPRINT_1.md) for the first Codex session.
