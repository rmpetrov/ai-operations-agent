# Architecture — deterministic backend, offline runtime/CLI/evaluation, and mocked provider adapter

## Purpose

Simulate three customer-support queues and provide a reliable, explainable source of
operational facts. **No live LLM, autonomous writes, automation, or irreversible action.**
Sprint 4B adds an internal runtime driven only by trusted scripted fake-model outcomes.
Sprint 4C adds an optional Responses transport verified with mocked provider responses only;
live model invocations remain unapproved.
Sprint 4D adds a local offline CLI that exercises the runtime using deterministic simulation
and actual authorized database facts. It cannot enable a provider or infer intent from questions.
Sprint 4E-1 adds versioned offline evaluations using isolated synthetic fixtures only.
Sprint 4E-2A prepares a separately gated Responses smoke runner with an offline default and mocked
transport tests only. Its actual live-execution path remains unexecuted and requires separate approval.

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
   Shared orchestration in `assessment.py`, exposed by `queue_services.py`, normalizes the
   single aware timestamp to UTC,
   collects facts once, evaluates risk once, and returns an immutable context. `main.py`
   maps missing queues to 404 and database failures to sanitized 503, and uses
   HTTP-independent `queue_services.py` builders for
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

Both routes use the same `main.py` dependency and `assessment.py` orchestration through
`queue_services.py`, which also owns their canonical response construction. One
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

## Scoped read-only tools (Sprint 4A)

This first Sprint 4 implementation adds an internal boundary for future callers. It adds no
LLM provider, adapter, agent loop, CLI, agent HTTP endpoint, authentication, mutation tool,
arbitrary SQL, shell, filesystem, or network tool. The local-only HTTP MVP remains unchanged.

### Shared services and contracts

`queue_services.py` owns the deterministic queue listing query and the assessment and
recommendation response builders extracted from routes. It exposes the existing
`assessment.py` pipeline and reuses `metrics.py`, `risk.py`, and `recommendations.py`.
Routes retain their Pydantic schemas and HTTP status/error contracts; write-capable routes
are unchanged. Tools call services directly, never HTTP endpoints. Recommendation calls
collect once (queue lookup plus one combined aggregate), evaluate once, and build once
using a single captured UTC timestamp and effective immutable policy.

`tool_schemas.py` defines strict, extra-forbidden arguments, queue identities, and canonical
recommendation results. Recommendation result schemas inherit the canonical fields and
traceability validators, adding strict nested assessment validation. `list_queues` accepts
only `{}` and returns an ID-ordered array with exactly `id` and `name`. The
`get_queue_recommendations` tool accepts exactly `{"queue_id": <strict positive integer>}`
and returns the existing `QueueRecommendations` envelope, including its complete assessment.
Neither tool loads individual ticket/agent objects or returns ticket identifiers or subjects.

### Server-owned access and execution limits

Only trusted server code constructs the frozen `AccessContext`. It holds explicit immutable
`allowed_queue_ids`, `allowed_tool_names`, optional `selected_queue_id`, and frozen
`ExecutionLimits`. Empty/missing queue or tool sets deny access. This is a capability boundary
for internal calls, not authentication or a safe deserializer for user permissions.
Questions and tool arguments cannot supply permissions, policy, clocks, or connection targets.
The only accepted names are `list_queues` and `get_queue_recommendations`; unsupported or
unpermitted names fail before database access. Strict positive IDs reject boolean, string,
float, zero, and negative values, and all unexpected argument fields are forbidden.

Discovery includes scope and optional selected-ID predicates in SQL, rather than retrieving
all rows and filtering in Python. Recommendation authorization occurs before queue lookup.
When a selected queue exists in the context, calls cannot switch to another queue even
if it is also allowed. Forbidden selection and nonexistent selection produce the identical
sanitized `queue unavailable` error; a selected nonexistent queue also fails discovery.
An unselected discovery with no existing authorized queues returns an empty array.

Arguments are dictionaries or JSON object strings, with duplicate JSON keys rejected,
bounded before schema validation to
4,096 UTF-8 bytes by default. Result snapshots default to at most 65,536 bytes and fail
as a whole if too large. PostgreSQL statement timeout defaults to 5,000 ms. Limits are
server-configured positive integers. Sprint 4A itself has no multi-step execution budget;
Sprint 4B layers the bounded runtime below over this dispatcher.
Database failures expose only `database unavailable`; validation errors do not echo inputs.

### Transaction ownership and read-only enforcement

`read_only.py` accepts an Engine, never an already active session or connection. Each call
owns its connection, transaction, and `ReadOnlySession`. Before any business query,
PostgreSQL explicitly uses Read Committed (including when given an autocommit engine),
then executes `SET TRANSACTION READ ONLY` and `SET LOCAL statement_timeout`.
Fixture writes in a separate transaction are never followed by a transaction mode change.
SQLite enables `PRAGMA query_only` before reads and restores the original connection
setting on exit, preserving StaticPool in-memory tests. Session autoflush is disabled;
explicit flush/commit and non-SELECT SQL (including Core DML and raw driver SQL) are rejected.
The transaction is rolled back and session/connection closed after success or failure. If
rollback or state restoration itself fails, the connection is invalidated rather than returned
to the pool with unknown state. Replacing a failed SQLite in-memory connection loses its
ephemeral database; a file-backed database retains its rows.

Native database rejection is tested by bypassing the Python SQL guard with a DBAPI cursor;
PostgreSQL returns SQLSTATE `25006`, and SQLite reports a read-only database. Tests also
verify pending dirty/new ORM values do not flush, database business rows remain unchanged,
transaction setup precedes business reads, and pooled connections regain their normal mode.
Existing PostgreSQL outer-transaction/savepoint fixtures are preserved. Separate tool fixtures
commit unique test rows only in the validated dedicated test database, remove exactly those
rows afterward, and compare the complete business-row baseline. Sequences may advance.

These are safeguards for fixed internal code, not a sandbox for arbitrary trusted Python
or user SQL. There is no separate least-privilege database role yet. SQLite's switch is
connection-wide, so shared in-memory connections must be used sequentially; SQLite has no
matching statement timeout. PostgreSQL Read Committed lookup/aggregate consistency limitations
and all existing assessment/recommendation policy limitations still apply.

### Detached results

`tools.execute_tool` strictly validates and serializes the result inside the session boundary,
then returns a fresh request-local JSON snapshot only after the session has closed. Nested
objects cannot leak live ORM/session state or mutate another invocation. The canonical
snapshot includes queue identity, timestamp, policy, metrics, signals, and typed recommendation
evidence. Sprint 4B assigns evidence IDs and validates references using that one snapshot.
Sprint 4A itself introduces no ledger or explanation; historical replay and provider calls
remain unimplemented.

Sprint 4B permits this offline runtime and its evaluations; Sprint 4C separately permits
only the opt-in provider adapter and mocked transport tests below. Live model calls,
live credential provisioning, autonomous writes, and public deployment still require
separate approval. Migration, local-only, and data-isolation safeguards remain in force.

## Offline agent runtime and deterministic evaluations (Sprint 4B)

`agent_schemas.py` defines strict frozen requests, outcomes, draft selections, references,
limits, and the versioned response. `fake_model.py` defines a narrow synchronous
`ModelAdapter` protocol and a `FakeModelAdapter` consuming trusted Python scripts.
`agent_runtime.py` owns all state and tool dispatch. `agent_evidence.py` owns a private
request-local ledger of serialized detached JSON snapshots. Sprint 4B introduced no new API route,
CLI, framework, persistence, or migration; Sprint 4D adds the local offline CLI below. The fake runtime has no SDK/secret/network
dependency; Sprint 4C adds an optional provider boundary below. **No live LLM is connected.**

### State transitions and routing boundary

Application code supplies the workflow, never permissions from a question. This is explicit
structured routing, not a free-text intent classifier. Unsupported forecast/staffing requests
must be routed as `unsupported`; that state cannot call tools or produce operational advice.
Model routing accuracy is distinct from server authorization: scripted routing tests demonstrate
known choices, while scope and selected-queue enforcement protect every actual dispatch.

| Initial workflow | First permitted tool | Next state | Terminal outcome |
|---|---|---|---|
| Discovery | `list_queues` | Discovery captured; no more tools | Complete authorized queue list |
| Selected assessment | `get_queue_recommendations` | Assessment captured; no more tools | Complete canonical recommendations |
| Named assessment | `list_queues` | Unique authorized name resolved, then `get_queue_recommendations` | Complete canonical recommendations |
| Unsupported | None | Structured inability only | `unsupported` |

An unselected assessment without a name returns `needs_clarification` without a model or
database call. After scoped discovery, names use exact stripped/casefolded matching:
zero matches returns `insufficient_information`, multiple matches `needs_clarification`.
Only a unique discovered queue can be requested. An explicit server-selected queue always
wins and cannot change. Even an otherwise authorized queue is rejected if it differs from
that target. Forbidden and nonexistent selections expose the same `queue unavailable` error.
Empty/missing scope denies access, and unauthorized queue requests perform zero business
reads for that queue. Selection never grants a permission absent from `AccessContext`.

Every turn yields exactly one typed `ToolRequest`, `StructuredAnswerDraft`, `Refusal`, or
`ModelFailure`. A batch is an invalid outcome. The state machine enforces at most **2 tool
requests and 3 model turns**, no repeated tool or call ID, sequential execution, no switching
after assessment, and immediate termination on an answer/failure. Limits may be lowered,
but these ceilings cannot be increased. Invalid outcomes, tool names/arguments, and
transitions are rejected before dispatch. There are no automatic retries or repair loops.

### Adapter, deadlines, and bounded data

`next_turn(conversation, permitted_tools, response_schema, deadline)` receives copies of
JSON conversation values, state-filtered permitted tool names, the draft schema, and an
absolute monotonic deadline. It receives no engine, session, permissions object, or ledger
capability. The fake consumes constructor-provided outcomes or raises scripted exceptions;
no user input is parsed as a script. Sprint 4C implements this protocol through the opt-in
Responses adapter below; live invocations still require separate approval.

The complete request defaults to 4,096 UTF-8 bytes, a turn to 8,192, and conversation/response
each to 196,608. Dispatcher argument/result limits remain 4,096/65,536. The default run
deadline is 10 seconds (maximum configurable 30), checked before/after adapter and tool calls
and before accepting rendered answers. Expired tool outputs cannot enter the ledger.
The tool's PostgreSQL statement timeout is capped by remaining monotonic time and the
existing server-owned statement limit. Clock and ID generation are injectable in tests.
These are cooperative synchronous checks: arbitrary blocking Python, pool connection waits,
and SQLite statements cannot be preempted by this runtime. The supplied fake never blocks;
these evaluations do not establish a hard wall-clock guarantee for a future provider.

### Evidence and rendering

Only successful `execute_tool()` calls feed the ledger; session closure and native read-only
transactions remain Sprint 4A responsibilities. Runtime code performs no SQL or HTTP calls.
Discovery uses one scoped SELECT; assessment uses the existing queue lookup and one combined
business aggregate. No resolution/rendering read or second aggregation is introduced.

The ledger copies complete canonical snapshots into immutable JSON strings, assigns unique
server IDs, and derives an allowlist of typed pointers: discovery `/<index>` entries,
`/assessment`, named `/assessment/metrics/<field>` values, and
`/recommendations/<index>` entries. Sources carry the current run ID. Resolution requires
the exact source/type/pointer and permitted queue inside that run. The adapter receives
separate copies; it cannot mutate stored facts. No arbitrary JSON pointers, ticket identities,
model-authored source content, cross-request sources, persistence, or database re-reads exist.

Drafts select only kind, typed references, canonical codes, fixed block types, and inability
category. They cannot supply prose or authoritative counts, actions, severities, priorities,
signals, timestamps, forecasts, identities, or payloads. Queue-focus rendering requires an
assessment plus all its recommendations in exact canonical order; invented, inactive,
duplicate, omitted, and reordered codes fail. References must include the full assessment
and each recommendation in order, with optional valid metrics afterward. Discovery references
must cover exactly the actual authorized list. The server builds operational prose from fixed
templates and the canonical snapshots. It never adds affected counts, infers intersections,
or represents empty recommendations as an operational-health guarantee.

`offline-agent-v1` responses include run/status, three literal safety flags, complete sources,
resolved evidence, rendered explanation blocks, uncertainty, and sanitized error. Sources and
resolved evidence intentionally duplicate bounded data for self-contained traceability.
The strict response validator checks run/source provenance, typed pointer resolution and
value equality, recommendation-reference completeness/order, and explanation citations.
The runtime additionally compares response sources/evidence with its private ledger; JSON
shape alone cannot establish provenance. No approval or execution state is exposed.

Errors have a fixed code/message allowlist; database, adapter, validation, and arbitrary
exception details are never echoed. Refusal returns structured inability. A failed run may
retain already successful source snapshots for audit but emits no operational conclusions or
resolved evidence. Oversized responses fail as a whole without partial/truncated sources.
All evidence disappears with the run; the response is not a persisted historical replay log.

### Offline evaluations and limitations

`tests/test_offline_agent_evaluations.py` runs full scripted transitions through real tools
on SQLite and isolated PostgreSQL, including the four workflows, missing/ambiguous/empty
queues, all four signals and no triggered rules, scope and routing failures, malicious-looking
questions, malformed outcomes/batches, bad arguments/tools, duplicate IDs/calls, budget and
size/deadline exhaustion, refusals/exceptions, evidence/code/prose fabrication, sanitization,
row equality, no flush/commit, closed sessions between turns, and single aggregation.
These are **offline deterministic evaluations**, not measurements of real-model routing
accuracy or hallucination frequency. No second model validates grounding.

All existing policy, Read Committed snapshot, aggregate-only, presence-freshness, lack of
authentication/least-privilege DB role, and local-only limitations remain. Trusted application
routing and scripts are inside the trust boundary; this is not a sandbox for arbitrary Python.
Production authentication, live provider cancellation/reliability, free-text intent routing,
historical persistence, forecasts, and any write/approval workflow remain future scope.

## Local offline CLI boundary (Sprint 4D)

```
Trusted local operator: DATABASE_URL / existing settings + AGENT_ALLOWED_QUEUE_IDS
                                     |
Explicit CLI command + selection -----+--> agent_cli: validation, AccessContext
                                                   |
                                                   v
                                               run_agent
                                              /         \
                              DeterministicDemoAdapter   existing execute_tool
                              (detached conversation)          |
                                                     read_only_session
                                                              |
                                                     canonical queue services
                                                              |
                                              SQLite / migrated PostgreSQL
                                                   |
                                     ledger + grounded runtime rendering
                                                   |
                              bounded terminal text / existing AgentResponse JSON
```

`python -m ai_operations_agent.agent_cli` provides only `queues`,
`assess --queue-id ID`, and `assess --queue-name NAME`. It is a local, synchronous,
single-process demonstration, with no listener, agent HTTP endpoint, provider-enabled
mode, autonomous writes, UI, framework, schema creation, seeding, or persistence.
`FakeModelAdapter` and `OpenAIResponsesAdapter` remain unchanged. The CLI never imports
or constructs a provider client or reads provider credentials/configuration.

### Local configuration and explicit routing

The local operator/environment is inside the trust boundary. The CLI creates a frozen
`AccessContext` from `AGENT_ALLOWED_QUEUE_IDS` in the process environment, never `.env`
or questions. This is a capability grant, not user authentication or independent proof
of an operator's authority. The setting accepts 1–32 distinct positive ASCII decimal
IDs (at most PostgreSQL Integer's 2,147,483,647), in at most 512 UTF-8 bytes. Whitespace
around tokens is ignored; missing/empty values, duplicates, wildcards, noncanonical or
out-of-range IDs, and excessive scope fail closed before any database access. The
32-queue ceiling fits the existing draft's maximum discovery reference count. Unknown
IDs expose no existing data; discovery may be empty, and unknown/forbidden selections
share the same controlled error. No unscoped fallback exists.

Only explicit command selection constructs the workflow. By-ID selection must belong
to the allowlist before the runtime starts. Tool grants are narrowed to discovery only,
assessment only, or both for name resolution. The runtime still independently enforces
all grants, selection, exact stripped/casefolded name matching, unique name resolution,
transitions, budgets, and references. Named assessment uses one scoped listing followed
by exactly one assessment; by-ID assessment skips discovery. There is no preliminary
database query or second evaluation. Unknown/ambiguous names stop before assessment.
Questions cannot enable a tool, change permissions/connection/policy, or configure a
provider. The optional question is unused demonstration text; arbitrary natural-language
intent is not supported. Queue names are at most 160 characters; the complete serialized
request must fit the existing 4,096-byte UTF-8 request limit.

Configuration-dependent imports are delayed until after argument/scope/request validation,
so help works without database configuration and missing/malformed settings produce a
controlled error without printing validation inputs. Existing local settings still own
the database target and risk policy. No `.env` file is created or modified.

### Simulation and output

`demo_model.DeterministicDemoAdapter` implements `ModelAdapter.next_turn()` without
an engine, session, dispatcher, provider, evaluation clock or policy lookup, or evaluation capability.
It uses only the explicit workflow, trusted selected ID, currently permitted tools,
and detached successful tool-result conversation. For named workflows it uses the
same unique matching rule as the runtime to propose the next selected tool argument;
the runtime remains authoritative. Fixed call IDs are per run, never source IDs.
Draft references come directly from the runtime-issued reference metadata and codes
from actual canonical recommendation snapshots. The adapter invents no counts,
recommendations, sources, tool results, or prose. The existing ledger/renderer validates
and renders every final answer. Simulation does not establish model intelligence.

The CLI displays existing explanation blocks and limitations, plus a deterministic
simulation label and literal read-only/advisory/human-review status. It escapes all
nonprintable Unicode characters inside lines, including C0/C1 controls, ANSI introducers,
bidi controls, and line separators. JSON uses ASCII escaping and the unchanged
`AgentResponse` contract; decoded evidence/source values are preserved. The evidence
view lists successful source IDs, tool names, and validated pointers. Sources are
**not a complete trace of failed/rejected calls**. Existing runtime/tool limits remain;
final rendered output also has a 196,608-byte limit after escaping, including its newline.
Oversize failure replaces the whole output with a small controlled error.

Exit codes distinguish answered (`0`), arguments/request (`2`), local configuration
(`3`), runtime/database/tool/output failure (`4`), and queue selection failure (`5`).
Argument errors do not echo untrusted values. Text errors use stderr; JSON execution
results use stdout. Failed runs show no operational conclusions or resolved evidence;
existing JSON can retain bounded prior successful sources. Raw exceptions, connection
strings, credentials, and private provider transcripts are never rendered.

`tests/test_agent_cli.py` reuses synthetic committed read-only fixtures and their exact
cleanup/baseline comparisons on SQLite and the isolated PostgreSQL test target. It covers
commands, real tools, all four canonical signals, empty/no-rule queues, scope/selection
denials, ambiguous names, question injection, control escaping, evidence/JSON, request
and output limits, sanitized failures, and exit codes. Spies and SQL assertions establish
one collection/evaluation/recommendation build, no duplicate aggregate query, and no
flush/commit or changed business rows. Provider imports/constructors and HTTP requests
are blocked; a separate SQLite subprocess also blocks socket connections and provider
credential reads. Test setup alone creates its temporary SQLite schema; the CLI does
not. No test seeds, resets, or modifies the development database. All Sprint 4A–4C and
Feature 001–003 invariants and limitations remain in force.

## OpenAI Responses transport boundary (Sprint 4C)

`openai_responses.py` implements the existing synchronous `ModelAdapter.next_turn()` protocol.
This is an approved adapter implementation and mocked-test milestone, not approval for live
model invocations. The SDK is an optional `openai>=2.54,<3` extra, tested with 2.54.0.
No application route, startup/health integration, framework, hosted tool, database schema,
migration, or autonomous write is introduced. Existing API and `offline-agent-v1` contracts,
the fake adapter, dispatcher, private evidence ledger, and server renderer remain intact.

### Configuration, lifetime, and budgets

Frozen `ResponsesSettings` defaults to disabled and requires an explicit model ID when
enabled. Trusted Python code alone selects the model, timeout, token limit, request ceiling,
and payload limits; questions/tool arguments cannot configure the adapter. It does not read
`.env`. Only the first enabled turn reads `OPENAI_API_KEY` from the environment, then lazily
imports the SDK and builds a client. Missing/blank credentials return sanitized failure.
The fixed official base URL prevents environment `OPENAI_BASE_URL` from redirecting secrets.
Client construction does not occur at import, startup, health checks, or fake evaluations.
No secret is part of settings, answer data, or adapter logs. SDK debug/external request
tracing must not be enabled with secrets or operational transcripts.

The optional `RunScopedModelAdapter.open_run()` lifecycle lets `run_agent()` obtain a new
adapter scope using `ExitStack` and guarantees cleanup on all exits. Existing adapters need
only `next_turn()` and retain their behavior. Each provider scope owns its client, accepted
output items, conversation prefix, pending call, seen call IDs, and request counter. Scope
closure closes the SDK client and discards history. Reusing a configured adapter or opening
overlapping scopes cannot share that state. Direct callers use a fresh scope per run;
attempting to restart a used scope fails closed. No cross-run cache or response ID exists.

The client has `max_retries=0`; each `responses.create()` uses `store=False`,
`parallel_tool_calls=False`, explicit model, and bounded `max_output_tokens` (default 2048,
ceiling 8192). Provider requests default to at most three and can only be lowered, alongside
runtime turn limits. The default ten-second timeout is capped by the remaining monotonic
deadline, rechecked after client/preparation work and after decoding. HTTPX timeouts apply
per network phase, not as hard process preemption. Late results fail, with no repair/retry.
Requests, responses, and complete replay history each have a 196608-byte default ceiling.
No live costs, model compatibility, cancellation reliability, or quality are established.

### Function and final-output conversion

Only the tools supplied by the current runtime state are advertised. Function schemas
require every property and forbid extras. Functions can only be `list_queues` and
`get_queue_recommendations`; no arbitrary SQL, hosted tools, or writes exist. A response
must contain exactly one function call or one structured assistant message, optionally
accompanied by reasoning. Unknown output types, batches, mixed calls/messages, incomplete
items/responses, and invalid/duplicate call IDs fail before dispatch.

Arguments remain raw JSON strings until Sprint 4A's parser checks UTF-8 size (default 4096)
and rejects duplicate keys while processing pairs. Nonobject/malformed JSON and extra
fields fail. Strict Pydantic tool validation rejects coerced queue IDs, including booleans,
strings, floats, null, and nonpositive IDs. The runtime revalidates dictionaries against
its own limits and authorizes the selected/resolved queue before any business read. SDK
parse helpers are not used. SDK serialization warnings are disabled to avoid echoing
malformed untrusted values; explicit application validation remains mandatory.

The internal draft schema includes optional/defaulted fields that strict provider schemas
cannot directly retain. A minimal provider schema uses a required object root, nested
extra-forbidden objects, enums, arrays, and nullable inability, with all seven fields
required and no defaults. It permits only answer kind, references, recommendation codes,
block kinds, and inability categories. Duplicate/extra/missing fields and invalid types
fail conversion to the original strict `StructuredAnswerDraft`. Counts, actions, operational
severity, evidence payloads, and arbitrary prose are excluded. Shape validity grants no
authority: the existing ledger/rendering pipeline still checks provenance, recommendation
membership/completeness/order, and all evidence before accepting an operational answer.

### Stateless continuation and failures

For each successful call, retain all accepted native output items in their original order,
including the function call and encrypted reasoning (`include: reasoning.encrypted_content`).
The next adapter turn accepts only the matching runtime conversation prefix, assistant
outcome, and successful tool record. It appends `function_call_output` with the provider's
exact `call_id`; output JSON contains the complete trusted source snapshot plus server
reference metadata. All earlier function outputs and reasoning are replayed on subsequent
turns. No `previous_response_id` or hosted conversation is used with `store=False`.
Model-authored function outputs are rejected and cannot become authoritative evidence.

Refusal text is discarded and converted to `Refusal`. Authentication/missing credentials,
quota/rate-limit, connection, invalid-request/unsupported-model, missing-SDK, and malformed
or incomplete response errors normalize to existing `ModelFailure(unavailable)`; provider
timeouts/deadline exhaustion normalize to `ModelFailure(timeout)`. The runtime retains its
existing `model_unavailable` / `deadline_exceeded` response codes. This intentionally avoids
expanding the public contract or leaking raw exceptions, headers, secrets, or transcripts.

`tests/test_openai_responses.py` uses the official SDK with `httpx.MockTransport` and blocked
real HTTP transport; it requires no real API key and is installed/run in CI on SQLite and
isolated PostgreSQL. Tests cover the full workflows, strict schemas/arguments, continuation
and correlation, failures/refusals, request/deadline/size budgets, isolation and cleanup,
grounding attacks, unchanged routes/health behavior, and read-only database invariants.
These tests verify transport integration and deterministic evidence controls only. **Live
model behavior and performance have not been evaluated.** Future approved live evaluations
must use synthetic operational data. Public deployment, live calls, new agent HTTP endpoints,
arbitrary tools/SQL, and autonomous writes remain outside this milestone.

## Reproducible evaluation harness (Sprint 4E-1)

`python -m ai_operations_agent.evaluation` is a separate offline entry point. It has no
provider/database URL/model/credential options and never routes questions. It loads strict,
frozen scenario contracts from packaged `evaluation_data/v1.json`, selects distinct scenario
IDs in dataset order, executes them, scores them, and writes ignored JSON and Markdown reports.
The existing demo and scripted fake adapters are its only adapters. Existing SDK `MockTransport`
tests additionally exercise Responses usage telemetry and independent scoring.

The dataset declares 26 scenarios in `task_success`, `security`, and `failure` categories. Each
specifies a trusted workflow/question, queue scope/selection, scripted behavior, allowed dispatched
sequences/variants with exact symbolic queue arguments, terminal status/error, literal recommendation
membership/order when applicable, and security invariants. Validation rejects duplicate IDs/scopes/codes,
unordered catalogs, missing security properties, inconsistent status/error expectations, and expected
dispatch outside scope/selection. Expectations are never imported from the engine or inferred from output.

`evaluation_fixtures.py` creates only private in-memory SQLite engines for runner use. Fixtures
include empty/healthy queues, isolated signal triggers, simultaneous signals, casefold-ambiguous
names, and a hidden queue. Fixed order, UTC evaluation time, and immutable `RiskPolicy` make SQLite
outcomes repeatable. A trusted optional `EvaluationContext` passes time/policy from runtime to tools
without changing tool schemas, HTTP dependencies, or default CLI behavior. No global monkeypatch,
development URL/reset/seed, or persistent database is used by the runner. Test-only `owned_fixtures`
also accepts the existing validated PostgreSQL fixture engine, uses unique namespaces and owned IDs,
cleans up in FK order, and compares all business rows with the pre-test baseline. Alembic still owns
the PostgreSQL schema; application startup never creates tables.

The evaluator invokes `run_agent` and the real dispatcher; it implements no risk/recommendation
rules. It compares dispatched names and type-sensitive canonical argument fingerprints with declared
variants, checks status/error, compares discovery identities and ordered recommendation codes with
independent expectations, revalidates `AgentResponse`, and matches source fingerprints to successful
ledger captures in the trace. Source scope/selection is checked separately. SQL observation covers
runtime only; a second guard records and blocks attempted non-read statements without retaining SQL
or parameters. Before/after business-row snapshots detect state changes. Fixture writes are excluded.

Optional `RuntimeTrace` is owned by trusted code and never sent to the adapter or added to
`AgentResponse`. It distinguishes model turns, adapter-emitted requests, runtime acceptance/rejection,
dispatch attempts, and successful ledger captures. Accepted calls can fail or expire without a source.
Rejected requests include invalid transitions, arguments, and authorization. Provider protocol items
rejected inside the Responses adapter are classified as model failures; they are not falsely reported
as runtime-emitted requests. This trace claims no visibility into provider reasoning. Known tool labels,
argument/snapshot hashes, counts, status, duration, and actual nonnegative token counts are the only
evaluation metadata. Raw arguments/questions/exceptions/provider output, encrypted reasoning, headers,
and credentials never enter reports. Hashes are comparison metadata, not authorization or a promise of
secrecy against guessing values.

Scoring reports scenario pass rate, normal-case task completion, tool selection, exact arguments,
grounding, recommendation completeness/order, unauthorized execution, writes/attempts, nonanswer rate,
and local elapsed mean/median/max. Rates include numerator/eligible denominator; absent applicability
is `null`. Tool/argument quality compares requests with legal workflow prefixes and includes deliberate
attacks. Negative scenario gates test expected rejection and absence of forbidden execution/claims;
expected refusal is a passing defense. Hard violations are separate from ordinary quality and
unexpected-error failures. Any required gate failure returns exit 1; invalid selection or execution/
reporting failure returns exit 2.

Responses telemetry counts each attempted SDK `responses.create`, including failures, and reads only
`input_tokens`, `output_tokens`, and `total_tokens` when valid fields exist. If any contributing request
lacks a field its aggregate remains unknown. Fake-only runs report zero provider requests and unknown
tokens. `evaluation_costs.py` is a read-only interface for a future externally verified, explicitly
versioned/model-specific price table with verification date and source. No prices or billing calls ship;
missing usage/pricing leaves cost unknown. Estimates are not provider spending caps. Live execution and
credential provisioning remain prohibited pending separate approval; future approved live evaluations
require synthetic data.

Reports record dataset/fixture/report versions, fixed fact-evaluation time, Git commit/dirty state
(unknown outside a checkout), adapter/model identity, per-scenario checks/events, metrics, violations,
timing/usage, and limitations. They omit operational snapshots and transcripts. Default reports are
Git-ignored. Determinism applies to scores/outcomes, not timing, Git state, or PostgreSQL IDs/namespaces.
Scripted results do not measure language routing, real-model hallucination frequency, provider
availability/latency, cost, or production readiness.

## Controlled live smoke preparation (Sprint 4E-2A)

`live_evaluation.py` adds a local entry point with an offline plan as its default. Its top-level
imports are limited to pure schemas, dataset/usage/cost contracts, and a request-budget object.
Backend configuration, runtime evaluation, fixture/database code, the Responses adapter, and SDK
are imported only after execution preconditions pass. Import/default dry-run cannot load API
credentials, read `.env`, construct clients/databases, make requests, or write reports. A fresh-process
test blocks network, credential access, dotenv opens, and backend/provider imports to verify this boundary.

Only three unchanged scenarios from `synthetic-agent-eval-v1` are used, in fixed order:
`authorized-discovery`, `selected-all-signals`, and `named-assessment`. Their original trusted workflows,
scopes, server selection, and independent expected results are preserved. Private in-memory SQLite is
always constructed internally; development/test URLs never select a connection. The existing synthetic
fixtures provide deterministic identities, records, time, and policy. `evaluation.evaluate_scenario`
now accepts optional trusted Python adapter/trace/runtime-limits/clock seams; its default offline
behavior, dispatcher, scorer, grounding rules, and read-only SQL observation are unchanged. There is
no second runtime, dispatcher, recommendation engine, or scorer, and the offline CLI is unchanged.

The future real-transport path requires all of: explicit `--live`, a process-only nonsecret opt-in
marker `AGENT_LIVE_SMOKE_OPT_IN=approved-synthetic-smoke-v1`, interactive input/output, exact confirmation
`RUN gpt-6-luna SYNTHETIC SMOKE`, enabled validated settings for the fixed `gpt-6-luna` model, and a
nonblank process API key. Missing consent/configuration or an automated context fails before key access
or client construction. Presence of `CI`, `GITHUB_ACTIONS`, `PYTEST_CURRENT_TEST`, or an imported pytest
module blocks the real transport. No flags accept keys, database/base URLs, models, questions, grants,
or arbitrary scenario IDs. These gates prevent accidental activation; they do not confer repository
approval. **This milestone does not authorize live execution or credential provisioning.**

Trusted tests may inject an official SDK client factory only through Python. Before any SDK request,
that seam requires an `httpx.Client` whose default and mounted transports are `httpx.MockTransport`;
an accidentally injected real transport is closed and rejected. No CLI option exposes it. Full mocked
workflows test the actual SDK, native reasoning-item replay, `function_call_output` correlation,
usage capture, runtime/tool/scorer calls, and resource cleanup. No raw reasoning is added to telemetry.

`provider_budget.ProviderRequestBudget` is an invocation-local, shared, atomic ceiling of at most nine
SDK requests across all scenarios; trusted code may lower it. It is passed through each Responses
adapter's private run scope without resetting the counter. Reservation occurs immediately before
`responses.create`, so authentication/rate-limit/network failures consume requests; missing credentials,
expired deadlines before dispatch, and exhausted budgets do not create invented requests. The existing
per-scenario three-request adapter ceiling, three model turns, and two serial tool calls remain. The
runner stops on the first failed scenario, observed security violation, or exhausted global budget,
without reruns, repair loops, retries, or recovery. Reaching a budget between scenarios leaves the
remaining scenarios explicitly unexecuted; it never reports them as successes.

Smoke output is capped at 2,048 tokens per request (including reasoning), with a nine-request output
ceiling of 18,432. Input-token usage remains unknown until actual usage fields arrive; payload bytes
are not a token/currency budget. The general runtime's ten-second default is unchanged. Smoke scenarios
default to a scoped 30-second deadline to accommodate up to three serial calls, selectable only as
10/20/30 seconds within existing validated bounds. Each SDK request is capped at ten seconds and the
remaining scenario time. Existing size limits remain: 196,608 bytes for provider payload/conversation/
response, 8,192 for model turns, 4,096 for question/arguments, 65,536 for tool results. These are local
cooperative controls, not hard process/provider cancellation or guaranteed provider billing caps.

Responses settings retain `store=False`, `parallel_tool_calls=False`, strict schemas, no hosted tools,
and zero SDK retries. The fixed official endpoint still ignores `OPENAI_BASE_URL`. The proposed model
is configured in trusted code only; no live account access/compatibility or latency was verified.
Official [model](https://developers.openai.com/api/docs/models/gpt-6-luna) and
[function-calling](https://developers.openai.com/api/docs/guides/function-calling) references document
the intended API surface; this milestone does not add price assumptions or query billing.

Reports retain existing sanitized evaluation principles, with workflow/model, model/tool request/
dispatch/rejection counts, actual provider requests, returned outcome and exact tool/argument checks,
grounding/provenance/recommendation checks, security violations, operational writes/attempts, per-case
and whole-run elapsed durations, actual token fields, and versioned-price estimates only when supplied
through the existing cost interface. Executed-case metrics use the existing aggregate function;
planned-task completion includes unexecuted scenarios as incomplete. Scoring/cleanup exceptions fail
the overall gates, keep observed request counts, and distinguish an unscored attempt from a scenario
that never ran; their security audit is marked incomplete rather than asserting a clean audit.
Blocked runs record zero executions and their sanitized reason. Missing usage/pricing
stays unknown rather than zero. Reports contain no snapshots, questions, raw provider outputs,
exceptions, encrypted reasoning, keys, headers, connection strings, or confidential row content.

Dry-run prints only its plan. Future execution writes JSON/Markdown beneath ignored
`evaluation-results/live-smoke/` by default. CLI exit 0 means dry-run or all execution gates passed;
1 means blocked/failed execution, and 2 means invalid options/report-write failure. Option errors do
not echo user values. The 26-scenario offline evaluator remains independent and retains its contracts.
No successful live experiment, language understanding, real provider latency/cost, zero-hallucination
claim, production reliability, or natural-language routing is established by this preparation.

## Future work (not implemented)

- SLA policy calendars, per-ticket effective due timestamps, reassignment audit history.
- Immutable event log for agent decisions and the actual changes they trigger.
- Authentication, roles, human approval, and a least-privilege database role.
- Approved live provider invocation, intent routing, and hard cancellation (separate approval).
- Metrics snapshots / event ingestion from a real external support tool.
- Observability, retries/idempotency, deployment and threat modeling.
- Real-model routing/quality evaluations based on deterministic operational facts.
