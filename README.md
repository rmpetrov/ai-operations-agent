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
and prints a skip message. To completely reset *local* data (DESTRUCTIVE):
`docker compose down -v`, then recreate the service and repeat migrations + seed.

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

Status transitions: `open -> in_progress -> resolved`, with `open -> resolved` also allowed.
Resolved tickets are terminal in this starter.

Example ticket creation:

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/tickets \
  -H 'Content-Type: application/json' \
  -d '{"external_id":"DEMO-101","subject":"Login issue","queue_id":1,"priority":"high"}'
```

## Tests and quality checks

```bash
pytest -q               # quick tests use isolated in-memory SQLite by default
ruff check .
```

Run the **same tests against migrated PostgreSQL** (uses transactions/savepoints and rolls
back each test; use a dedicated disposable development database for safety):

```bash
TEST_DATABASE_URL="$DATABASE_URL" pytest -q
```

Important: the shell must export `DATABASE_URL` for the command above, e.g.
`export DATABASE_URL="$(sed -n 's/^DATABASE_URL=//p' .env.example)"`.
The GitHub Actions workflow runs Alembic + seed + integration tests against PostgreSQL.

## Learning plan

1. Explain schema ownership and migration history (`alembic/versions/0001_initial_schema.py`).
2. Trace a POST request from Pydantic validation to transaction commit.
3. Write a new failing test for a business rule before implementing the change.
4. Add an endpoint to show tickets by age bucket, with tests for all boundaries.
5. Only after the deterministic baseline is solid, add an LLM that can *recommend*
   actions, with mandatory server-side approval and checks.

See [architecture notes](docs/ARCHITECTURE.md) for invariants and limitations,
and [Sprint 1 learning exercises](docs/SPRINT_1.md) for the first Codex session.
