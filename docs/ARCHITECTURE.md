# Architecture — first milestone

## Purpose

Simulate three customer-support queues and provide a reliable, explainable source of
operational facts. **No LLM, agent, automation, or irreversible action yet.**

## Context

- **Queue:** a support team's workstream with a target resolution time in minutes.
- **Agent:** a human support operator belonging to exactly one queue.
- **Ticket:** a support request associated with one queue and optionally assigned to one agent.
- **Queue metrics:** the current number of active, unassigned, and already-overdue tickets,
  plus the count of online human operators.

```
Client / Swagger UI
       |
       v
FastAPI routes + Pydantic validation
       |
       v
SQLAlchemy Session (one session per request)
       |
       v
PostgreSQL 16 (schema owned by Alembic)
```

## Architectural decisions

1. **Synchronous SQLAlchemy 2.x:** the operations are short, database-bound requests.
   This keeps the learning scope focused on transaction boundaries, schemas and data correctness.
2. **Alembic migrations:** schema changes must be versioned; no application-side `create_all()`.
3. **Hard database invariants:** priority, state, SLA, and resolved timestamps are constrained
   even if a request bypasses the public API.
4. **Read-only metrics:** calculate from stored tickets instead of asking a model to infer facts.
5. **Local-only MVP:** sample DB passwords and unauthenticated mutation endpoints are
   intentionally not appropriate for a public deployment.
6. **Small surface:** queues and agents are read-only in the initial API; ticket create/transition
   are the only write operations.

## SLA definition and caveat

`sla_breached_tickets` is the number of **currently open or in-progress** tickets with
`created_at < calculation_time - queue.sla_minutes`. This is a simplified age-based breach metric.
It is **not** an SLA risk prediction, first-response SLA, business-hours SLA, or historical SLA
compliance metric. These require a richer data model and a policy definition in later milestones.

## Queue risk assessment (Sprint 2 / Feature 002)

`GET /api/v1/queues/{queue_id}/risk-assessment` adds three responsibilities:

1. `metrics.py` reads queue identity/SLA and then executes one SQL aggregate statement.
   Conditional ticket counts and an independent scalar online-agent count avoid join
   multiplication. It returns immutable facts and does not flush or commit; autoflush
   is disabled during collection. Tickets are never loaded individually for assessment.
2. `risk.py` contains immutable policy, facts, and evaluation values plus pure signal rules.
   It has no FastAPI, SQLAlchemy, clock access, or I/O. Identical facts, policy, and warning
   duration always produce identical ordered signals and maximum severity.
3. `main.py` injects the session, validated policy, and `clock.py` evaluation dependency,
   normalizes that single aware timestamp to UTC, maps missing queues to 404 and database
   failures to sanitized 503, and validates dedicated Pydantic response models in `schemas.py`.
   Noninteger path IDs return 422; zero/negative unknown integer IDs return 404.

There are no internal HTTP requests, schema changes, migrations, new dependencies, writes,
notifications, autonomous actions, scores, forecasts, staffing recommendations, or LLM calls.
The existing metrics endpoint retains its contract.

### Policy and business rules

Active means `open` or `in_progress` in the requested queue. Let `T` be the evaluation time,
`S` be its current SLA duration, `D = created_at + S`, and `W = S × remaining_percent / 100`.
Integer microsecond duration arithmetic preserves fractional-second windows. Comparing
`created_at` against UTC cutoffs in SQL avoids database-specific timestamp arithmetic and
maintains the same microsecond boundaries in SQLite and PostgreSQL.

| Ordered signal | Exact trigger | Severity | Affected count |
|---|---|---|---|
| `SLA_BREACHED` | At least one nonfuture active ticket with `D < T` | `high` | Breached tickets |
| `SLA_AT_RISK` | At least one nonfuture active ticket with `T ≤ D ≤ T + W` | `medium` | At-risk tickets |
| `UNASSIGNED_BACKLOG` | Null-assignment active count ≥ configured minimum | `low` | Unassigned tickets |
| `NO_AGENTS_ONLINE` | Active count > 0 and recorded online-agent count = 0 in this queue | `medium` | All active tickets |

`D = T` is at risk, not breached. SLA buckets are disjoint. A ticket created after `T`
is included in active and (if null-assigned) unassigned backlog, counted separately in
`future_created_active_tickets`, and excluded from both SLA buckets. Resolved tickets are
excluded from all ticket counts. Agent presence depends only on recorded queue and status.
Assignment means a nonnull agent ID, including offline agents; cross-queue assignment integrity
remains the existing API's responsibility.

`config.py` validates `RISK_SLA_REMAINING_PERCENT` (integer 1–99, default 20) and
`RISK_UNASSIGNED_MIN_TICKETS` (integer ≥ 1, default 1) at startup. The immutable policy
also validates its inputs. The defaults expose the final fifth of the SLA and any assignment
gap; these are starting policy choices without empirical handling-time calibration.

Every response includes policy version `queue-risk-v1`, the remaining percentage, effective
warning window in seconds, and minimum unassigned count. Signal severities are fixed.
Overall severity is `max(signals)` under `none < low < medium < high`; multiple medium
signals remain medium. Empty/resolved-only queues produce no signals and severity `none`,
while retaining their online-agent count. Overlapping affected counts must not be summed.
The response contract and example are in the [README](../README.md#queue-risk-assessment).

### Time, consistency, testing, and limitations

The injected clock is captured once per request and passed explicitly through collection;
naive internal times are rejected. Tests override it with a fixed UTC value and scope overrides
with pytest cleanup. SQLite fixtures persist UTC because SQLite drops stored timezone offsets;
PostgreSQL tests additionally verify aware offset timestamps across a daylight-saving fallback.
The same API assertions and exact counts run against both database paths. Tests cover
microsecond deadline/window edges, fractional-second policies, error sanitization, and SELECT-only
collection with no explicit or implicit flush/commit, including a pending dirty ORM object.

Ticket and agent aggregates share one statement snapshot. Queue/SLA lookup is a separate
statement under PostgreSQL Read Committed, so a concurrent SLA change or queue deletion can
race with collection. A fixed evaluation clock does not provide an atomic historical view.
There is no persisted assessment or replay log. Determinism requires identical facts and policy.

Severity expresses rule priority, not outage impact or a probability of missed resolution.
Agent presence has no freshness timestamp and cannot establish capacity. Deadlines use the
current queue SLA; business calendars, pauses, ticket priority, and historical effective policy
are absent. Future-created counts expose one data-quality issue without proving other data
valid. `none` means no configured rule triggered, without a guarantee of operational health.

## Future work (not implemented)

- SLA policy calendars, per-ticket effective due timestamps, reassignment audit history.
- Immutable event log for agent decisions and the actual changes they trigger.
- Authentication, roles, human approval, per-tool permission checks.
- Metrics snapshots / event ingestion from a real external support tool.
- Observability, retries/idempotency, deployment and threat modeling.
- Agent planning + evaluations based on deterministic operational facts.
