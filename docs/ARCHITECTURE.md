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

## Future work (not implemented)

- SLA policy calendars, per-ticket effective due timestamps, reassignment audit history.
- Immutable event log for agent decisions and the actual changes they trigger.
- Authentication, roles, human approval, per-tool permission checks.
- Metrics snapshots / event ingestion from a real external support tool.
- Observability, retries/idempotency, deployment and threat modeling.
- Agent planning + evaluations based on deterministic operational facts.
