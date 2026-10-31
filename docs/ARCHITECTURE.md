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
3. `main.py` injects the session, validated policy, and `clock.py` evaluation dependency.
   Shared orchestration in `assessment.py` normalizes that single aware timestamp to UTC,
   collects facts once, evaluates risk once, and returns an immutable context. `main.py`
   maps missing queues to 404 and database failures to sanitized 503, and constructs
   dedicated Pydantic response models in `schemas.py`.
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

## Action recommendations (Sprint 3 / Feature 003)

`GET /api/v1/queues/{queue_id}/recommendations` returns advisory suggestions together
with the complete existing `QueueRiskAssessment` under `assessment`. The Sprint 2 route
and response contract remain unchanged. The envelope adds version `queue-recommendations-v1`,
literal `advisory_only: true`, literal `requires_human_review: true`, and `recommendations`.
No triggered recommendation means HTTP 200 with an empty list and the full assessment.

### Shared pipeline and deterministic catalog

Both routes use the same `main.py` dependency and `assessment.py` orchestration. One
server-controlled timestamp and one effective `RiskPolicy` produce one `QueueRiskFacts`
and one `RiskEvaluation`. A frozen context carries these exact values through response
construction and recommendation generation. There is no second clock/policy lookup,
collection, risk evaluation, or internal HTTP request. Business data reads remain one
queue lookup plus one combined aggregate, with no per-ticket loading or additional queries.

`recommendations.py` is pure: it accepts metrics, policy, warning duration, and the emitted
risk evaluation; it does not access a session, clock, HTTP client, or external service.
It maps only emitted signal codes and does not independently evaluate thresholds. Its
explicit catalog order is priority order with a fixed medium-priority tie break:

| Order | Code | Required signal | Priority | Exact suggested action |
|---|---|---|---|---|
| 1 | `REVIEW_OVERDUE_TICKETS` | `SLA_BREACHED` | `high` | Review overdue active tickets and decide the next response. |
| 2 | `REVIEW_UPCOMING_DEADLINES` | `SLA_AT_RISK` | `medium` | Review active tickets due now or within the warning window. |
| 3 | `VERIFY_RECORDED_AGENT_STATUS` | `NO_AGENTS_ONLINE` | `medium` | Verify recorded agent status and confirm actual availability. |
| 4 | `TRIAGE_UNASSIGNED_TICKETS` | `UNASSIGNED_BACKLOG` | `low` | Review unassigned active tickets and decide whether assignment is appropriate. |

There is at most one suggestion per code; repeated identical source signals are deduplicated,
and conflicting duplicate signals fail rather than selecting arbitrary evidence. Input signal
order does not affect recommendation order. Overdue and at-risk reviews coexist because
their SLA buckets are disjoint. Unassigned triage and recorded-status verification address
different concerns and can coexist with either SLA review. Priority describes preferred
human review order, without predictions of impact, probability, or required staffing.

The explanation is the exact source signal reason, preserving count grammar and fractional
window formatting. Recorded-status verification additionally appends the fixed sentence
"Recorded status does not establish actual availability." Fixed templates, mapping, and
ordering belong to the versioned recommendation policy; there are no new configurable thresholds.

### Typed evidence and traceability

Each suggestion contains a stable `code`, fixed `priority`, fixed single-element
`triggered_by_signals` array, `suggested_action`, `explanation`, and code-specific evidence:

| Code | Complete evidence schema |
|---|---|
| `REVIEW_OVERDUE_TICKETS` | `sla_breached_tickets`: integer > 0 |
| `REVIEW_UPCOMING_DEADLINES` | `sla_at_risk_tickets`: integer > 0; `sla_at_risk_window_seconds`: finite number > 0 |
| `VERIFY_RECORDED_AGENT_STATUS` | `active_tickets`: integer > 0; `online_agents`: integer = 0 |
| `TRIAGE_UNASSIGNED_TICKETS` | `unassigned_tickets`: integer > 0; `unassigned_backlog_min_tickets`: integer ≥ 1 |

`schemas.py` discriminates recommendation variants by code and rejects extra recommendation
or evidence fields, incorrect priorities/triggers, noninteger counts, and invalid ranges.
Recommendation and evidence values are frozen. Envelope validation rejects duplicate codes,
requires every trigger in `assessment.signals`, compares all evidence counts to assessment
metrics, and compares evidence thresholds to assessment policy. Queue identity, evaluation
time, SLA, and policy are inherited from the one nested assessment. Unsupported evidence
cannot silently pass through. See the [complete JSON example](../README.md#action-recommendations-sprint-3--feature-003).

### Safety, testing, and limitations

These are suggestions for human review, with no assignment, status update, notification,
approval state, persistence, execution, external call, or LLM integration. The builder has
no action tools. The existing unauthenticated mutation API still requires local-only use;
advisory flags are not authorization controls.

Pure tests cover every signal combination, exact text, fractional policy evidence,
deduplication, deterministic order, and rejected schema variants. API tests cover exact
responses, Sprint 2 parity, thresholds, microsecond boundaries, future-created tickets,
offline assignments, cross-queue isolation, DST offsets, errors, and literal advisory flags.
Spies verify single clock/policy/collector/evaluator/builder use and identical inputs.
SQL checks focus on business queries, comparing small and larger backlogs to catch repeated
collection or N+1 reads. Dirty ORM objects plus flush/commit guards and before/after row
comparisons verify read-only behavior. Existing SQLite and isolated PostgreSQL fixtures are
reused without changing their migration ownership or transaction safeguards.

All Sprint 2 limitations still apply. Recorded presence has no freshness or capacity evidence.
Future-created tickets cannot trigger SLA suggestions but remain eligible for backlog-based
suggestions under existing rules. Aggregate facts cannot identify affected ticket/agent IDs
or establish intersections across signals; affected counts must not be summed. No business
calendar, handling estimates, forecasts, historical replay, or atomic historical snapshot is
provided. Independent requests can differ, so recommendations must be presented with their
included assessment. Empty recommendations mean only that no configured rule triggered.

## Future work (not implemented)

- SLA policy calendars, per-ticket effective due timestamps, reassignment audit history.
- Immutable event log for agent decisions and the actual changes they trigger.
- Authentication, roles, human approval, per-tool permission checks.
- Metrics snapshots / event ingestion from a real external support tool.
- Observability, retries/idempotency, deployment and threat modeling.
- Agent planning + evaluations based on deterministic operational facts.
