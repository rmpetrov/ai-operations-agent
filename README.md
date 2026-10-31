# AI Operations Agent

A backend-first learning project: build the deterministic operations system **before**
introducing LLM agents. The first version models support queues, human operators,
and tickets, with a small metrics API and repeatable tests.

> **Status:** Sprint 3 — deterministic advisory action recommendations. Local development only; no LLM, no authentication, no public deployment.

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
curl http://127.0.0.1:8000/api/v1/queues/1/risk-assessment
curl http://127.0.0.1:8000/api/v1/queues/1/recommendations
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
| GET | `/api/v1/queues/{queue_id}/risk-assessment` | Read-only queue facts and deterministic risk signals |
| GET | `/api/v1/queues/{queue_id}/recommendations` | Advisory human actions with their complete supporting assessment |

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

## Queue risk assessment

`GET /api/v1/queues/{queue_id}/risk-assessment` uses one server-controlled UTC
`evaluated_at` timestamp. A hypothetical 30-minute queue can return:

```json
{
  "queue_id": 1,
  "queue_name": "Billing",
  "evaluated_at": "2026-10-31T16:00:00Z",
  "sla_minutes": 30,
  "policy": {
    "version": "queue-risk-v1",
    "sla_at_risk_remaining_percent": 20,
    "sla_at_risk_window_seconds": 360,
    "unassigned_backlog_min_tickets": 1
  },
  "metrics": {
    "active_tickets": 2,
    "unassigned_tickets": 1,
    "sla_breached_tickets": 1,
    "sla_at_risk_tickets": 1,
    "online_agents": 0,
    "future_created_active_tickets": 0
  },
  "overall_severity": "high",
  "signals": [
    {"code": "SLA_BREACHED", "severity": "high", "affected_tickets": 1,
     "reason": "1 active ticket is past its SLA deadline."},
    {"code": "SLA_AT_RISK", "severity": "medium", "affected_tickets": 1,
     "reason": "1 active ticket is due now or within 360 seconds."},
    {"code": "UNASSIGNED_BACKLOG", "severity": "low", "affected_tickets": 1,
     "reason": "1 active ticket has no assigned agent."},
    {"code": "NO_AGENTS_ONLINE", "severity": "medium", "affected_tickets": 2,
     "reason": "No agents are recorded online for this queue's 2 active tickets."}
  ]
}
```

Only `open` and `in_progress` tickets in the requested queue count as active.
A deadline is `created_at + queue.sla_minutes` in elapsed time. Rules run in this fixed order:

| Signal | Trigger | Severity |
|---|---|---|
| `SLA_BREACHED` | At least one active deadline strictly before `evaluated_at` | `high` |
| `SLA_AT_RISK` | At least one active deadline in the inclusive interval from `evaluated_at` to `evaluated_at + warning window` | `medium` |
| `UNASSIGNED_BACKLOG` | Unassigned active tickets meet or exceed the configured minimum | `low` |
| `NO_AGENTS_ONLINE` | Positive active backlog and zero agents recorded online in this queue | `medium` |

The deadline instant is at risk; a deadline one microsecond earlier is breached.
Future-created active tickets count toward backlog and unassigned counts, appear separately
in `future_created_active_tickets`, and are excluded from both SLA buckets. Resolved tickets
are excluded from every ticket count. Signals can overlap: do not sum affected counts.
An assigned ticket remains assigned even if its agent is offline.

Configure thresholds through environment variables or the local `.env` file:

| Variable | Default | Accepted values |
|---|---|---|
| `RISK_SLA_REMAINING_PERCENT` | `20` | Integer 1–99; final percentage of the queue SLA |
| `RISK_UNASSIGNED_MIN_TICKETS` | `1` | Integer ≥ 1 |

Invalid settings fail application startup. The warning window uses exact duration arithmetic:
20% of 30 minutes is 360 seconds; 1% of one minute is 0.6 seconds. Every response includes
the effective thresholds and window. Severities and the other two trigger thresholds are fixed.

Overall severity is the maximum triggered level: `none < low < medium < high`.
Multiple medium signals remain medium. Empty and resolved-only queues return no signals
and severity `none`, retaining the actual online-agent count. `none` means no rule triggered.
Unknown integer queue IDs (including zero and negative IDs) return 404; malformed IDs return 422.
Database errors return 503 with `{"detail":"database unavailable"}`, without fabricated counts.

This is deadline proximity and observed backlog, without a risk score, probability, forecast,
or staffing recommendation. Recorded presence has no freshness timestamp. SLA policy has no
business hours, pauses, or priority weighting; changing the queue SLA reclassifies existing
tickets. Assessments are not persisted for historical replay. Ticket/agent counts share one
SQL statement snapshot; the earlier queue lookup is separate, so `evaluated_at` does not
promise an atomic historical snapshot. See [architecture notes](docs/ARCHITECTURE.md).

## Action recommendations (Sprint 3 / Feature 003)

`GET /api/v1/queues/{queue_id}/recommendations` maps emitted risk signals to fixed,
human-review-only suggestions. It includes the complete existing `QueueRiskAssessment`
under `assessment`; the Sprint 2 endpoint and its response remain unchanged. For example,
one overdue, assigned ticket with one agent recorded online can produce:

```json
{
  "recommendation_policy_version": "queue-recommendations-v1",
  "advisory_only": true,
  "requires_human_review": true,
  "assessment": {
    "queue_id": 1,
    "queue_name": "Billing",
    "evaluated_at": "2026-10-31T16:00:00Z",
    "sla_minutes": 30,
    "policy": {
      "version": "queue-risk-v1",
      "sla_at_risk_remaining_percent": 20,
      "sla_at_risk_window_seconds": 360,
      "unassigned_backlog_min_tickets": 1
    },
    "metrics": {
      "active_tickets": 1,
      "unassigned_tickets": 0,
      "sla_breached_tickets": 1,
      "sla_at_risk_tickets": 0,
      "online_agents": 1,
      "future_created_active_tickets": 0
    },
    "overall_severity": "high",
    "signals": [{
      "code": "SLA_BREACHED",
      "severity": "high",
      "affected_tickets": 1,
      "reason": "1 active ticket is past its SLA deadline."
    }]
  },
  "recommendations": [{
    "code": "REVIEW_OVERDUE_TICKETS",
    "priority": "high",
    "triggered_by_signals": ["SLA_BREACHED"],
    "suggested_action": "Review overdue active tickets and decide the next response.",
    "explanation": "1 active ticket is past its SLA deadline.",
    "evidence": {"sla_breached_tickets": 1}
  }]
}
```

The following table gives the exact output order, fixed priorities, source signals,
and complete evidence fields. Each triggered signal produces one recommendation.
Evidence counts are positive integers except `online_agents`, which must be zero;
the warning window is a positive finite number of seconds and the minimum is a positive integer.

| Order | Recommendation | Priority | Trigger | Typed evidence fields |
|---|---|---|---|---|
| 1 | `REVIEW_OVERDUE_TICKETS` | `high` | `SLA_BREACHED` | `sla_breached_tickets` |
| 2 | `REVIEW_UPCOMING_DEADLINES` | `medium` | `SLA_AT_RISK` | `sla_at_risk_tickets`, `sla_at_risk_window_seconds` |
| 3 | `VERIFY_RECORDED_AGENT_STATUS` | `medium` | `NO_AGENTS_ONLINE` | `active_tickets`, `online_agents` |
| 4 | `TRIAGE_UNASSIGNED_TICKETS` | `low` | `UNASSIGNED_BACKLOG` | `unassigned_tickets`, `unassigned_backlog_min_tickets` |

The builder uses emitted signals rather than recalculating thresholds. It deduplicates
codes and retains both overdue and upcoming reviews when both signals exist. Priority
is the policy's preferred human review order, without predicted business impact or probability.
The original assessment signal order is preserved inside `assessment`.

Every recommendation has a fixed `suggested_action`, an `explanation`, and a single-element
`triggered_by_signals` array. Explanations reuse the source signal's reason; recorded-status
verification appends: "Recorded status does not establish actual availability."
Its suggested action is "Verify recorded agent status and confirm actual availability."
Recorded offline status does not prove staff unavailability; recorded online status does
not establish capacity. No staffing levels, specific ticket/agent IDs, or SLA forecasts are inferred.

Code-specific evidence schemas reject extra fields and invalid values. Response validation
requires each trigger to appear in the nested assessment, each evidence count to match its
metric, and each evidence threshold to match the effective policy. All suggestions share
one queue, UTC evaluation time, policy, facts collection, and risk evaluation. Collection
performs one queue lookup and one aggregate statement; generation adds no queries or HTTP calls.

Empty, resolved-only, and other queues with no triggered rules return HTTP 200 with
`recommendations: []`, the full assessment, and both advisory flags set to true. This does
not certify operational health. Missing integer queue IDs return 404, malformed IDs return
422, and database failures return sanitized 503, as for risk assessment.

This endpoint performs no writes, assignments, status updates, notifications, approvals,
execution, persistence, or external/LLM calls. Future-created active tickets remain in backlog
counts but cannot trigger SLA recommendations. A ticket may contribute to multiple suggestions;
do not sum affected counts or infer that the same tickets are both overdue and unassigned.
Presence freshness, business-hour SLA rules, an atomic historical snapshot, and historical
replay remain unavailable. Independent requests may observe different facts; use the included
assessment when displaying recommendations. Keep this unauthenticated MVP local only.

## Tests and quality checks

```bash
env -u TEST_DATABASE_URL .venv/bin/python -m pytest -q  # in-memory SQLite
.venv/bin/python -m ruff check .
git diff --check
```

Run the **same API and Feature 001–003 tests against migrated PostgreSQL 16**. The `db_test`
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
