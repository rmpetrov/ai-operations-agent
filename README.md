# AI Operations Agent

A backend-first learning project: build the deterministic operations system **before**
introducing LLM agents. The first version models support queues, human operators,
and tickets, with a small metrics API and repeatable tests.

> **Status:** Sprint 4D — local offline CLI demonstration using deterministic simulation and actual read-only database tools. The Sprint 4C provider adapter is verified through mocked SDK transport only. Live model calls remain unapproved. Local development only; no authentication or public deployment.

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

## Internal read-only tools (Sprint 4A)

Trusted Python server code can call exactly two tools through `tools.execute_tool`:

| Tool | Strict arguments | Detached JSON result |
|---|---|---|
| `list_queues` | `{}` | `[{"id": 2, "name": "Billing"}]`, ordered by ID |
| `get_queue_recommendations` | `{"queue_id": 2}` | The canonical `QueueRecommendations` structure shown above |

Sprint 4A introduced these internal functions without a new HTTP endpoint, CLI, live provider,
or external tool. Sprint 4B adds the internal offline runtime described below.
No API credentials or new dependencies are needed.
An internal call from trusted server code, using queue IDs authorized by that server:

```python
from ai_operations_agent.tool_schemas import AccessContext
from ai_operations_agent.tools import execute_tool

access = AccessContext(
    allowed_queue_ids=frozenset({2}),
    allowed_tool_names=frozenset({"list_queues", "get_queue_recommendations"}),
    selected_queue_id=2,
)
queues = execute_tool("list_queues", {}, access)
recommendations = execute_tool("get_queue_recommendations", {"queue_id": 2}, access)
```

Do not construct access permissions from a question or tool arguments. `AccessContext` and
its execution limits are immutable, with immutable queue/tool sets. Missing or empty queue
scope and missing tool permissions deny access. Discovery filters IDs in SQL; an explicit
selected queue also narrows discovery and cannot be changed by a recommendation call.
Forbidden and nonexistent queues return the same `ToolError("queue unavailable")`.
Unsupported tools and forbidden queue IDs fail before opening a database session.

Arguments accept a Python dictionary or a JSON object string. Extra or duplicate JSON fields and queue IDs
that are booleans, strings, floats, zero, or negative integers are rejected. Default limits
are 4,096 UTF-8 argument bytes, 65,536 result bytes, and a 5,000 ms PostgreSQL statement
timeout. Oversized results fail without returning a partial snapshot. Outputs contain only
validated queue identity or aggregate recommendation data, with no individual tickets,
subjects, external IDs, ORM objects, or sessions. Each call returns fresh JSON-compatible
values after its business session closes. The Sprint 4B runtime assigns evidence IDs to these
snapshots without re-querying.

`queue_services.py` supplies HTTP-independent listing, assessment orchestration, and
canonical response builders to both routes and tools. Existing HTTP response contracts and
write endpoints are preserved. Recommendation collection still uses one queue lookup and
one combined ticket/agent aggregate, one timestamp, one policy, and one risk evaluation.

Tools own short-lived sessions separate from HTTP and write-fixture sessions. PostgreSQL
sets transaction read-only mode and a local statement timeout before business queries, then
rolls back and returns the connection on success or failure. SQLite uses `query_only` and
restores the prior connection setting for in-memory/pool compatibility. Failed rollback or
state restoration invalidates the connection so it cannot leak unsafe state into the pool.
Both paths disable autoflush, reject explicit flush/commit and non-SELECT SQL, and leave
business rows unchanged.
The guard is for these fixed internal tools, not a sandbox for arbitrary Python or SQL.
SQLite has no matching statement timeout; its connection-wide switch requires sequential
use of a shared in-memory connection. PostgreSQL retains the existing Read Committed
consistency limits between queue lookup and aggregate. No dedicated least-privilege database
role, user authentication, or public access boundary is introduced here.

## Offline agent runtime (Sprint 4B)

**No live LLM is connected.** `agent_runtime.run_agent` is an internal synchronous Python
entry point, with no agent API endpoint. Sprint 4D adds the local offline CLI below. Trusted application/test code supplies an
`AgentRequest`, the existing server-owned `AccessContext`, and a `FakeModelAdapter` script.
Questions cannot carry scripts or permissions. The fake adapter needs no provider SDK,
model network call, or secret. Sprint 4C adds the optional adapter below without changing
HTTP contracts, migrations, deterministic tools, or rendering.

The application explicitly chooses `discovery`, `queue_assessment`, or `unsupported`.
This milestone does not classify free-text questions. Application routing must mark
forecast/staffing requests unsupported; the runtime rejects operational answers and tools
for that workflow. Selected queues come exclusively from `AccessContext`. Without a
selected queue, an assessment needs a queue name and successful scoped discovery; exact
matching ignores case and surrounding whitespace. Missing matches return
`insufficient_information`; multiple matches or a missing name return `needs_clarification`.
No first-match fallback is used. A model request for another authorized queue still fails
if it differs from the resolved name or explicit selection. Authorization remains a
separate server check, independent of model routing.

| Workflow | Permitted sequence |
|---|---|
| Discovery | `list_queues` → validated discovery answer |
| Server-selected assessment | `get_queue_recommendations` → validated queue answer |
| Named assessment | `list_queues` → `get_queue_recommendations` → validated queue answer |
| Unsupported capability | Structured inability, with zero tool calls |

Hard ceilings are **2 serial tool requests and 3 model turns**, with exactly one outcome
per turn. Duplicate IDs, repeated discovery/assessment, batches, wrong arguments, unsupported
tools, and invalid transitions fail before tool execution. Final answers end the run;
there are no retries or repair loops. Each tool retains its own read-only transaction and
closes it before the next simulated model turn.

The adapter contract is `next_turn(conversation, permitted_tools, response_schema, deadline)`.
It receives detached JSON and an absolute monotonic deadline and returns one `ToolRequest`,
`StructuredAnswerDraft`, `Refusal`, or `ModelFailure`. `FakeModelAdapter` consumes only a
trusted Python script. The Sprint 4C adapter implements this contract; live invocations
still need separate approval. Synchronous deadlines have the limitations below.

Defaults bound the complete request to 4,096 UTF-8 bytes, each model outcome to 8,192 bytes,
the conversation and response each to 196,608 bytes, and elapsed execution to 10 seconds
(configurable up to 30). Existing tool limits remain 4,096 argument bytes and 65,536 result
bytes. Runtime PostgreSQL statement timeouts are reduced to the remaining time budget.
The monotonic clock is injectable; results arriving after the deadline cannot become evidence.
Checks are cooperative around synchronous operations, not thread/process preemption; pool
connection waits and SQLite statements have no run-wide cancellation facility.

The request-local ledger stores immutable serialized copies of complete successful tool
snapshots, each with a unique server-generated source ID. Allowed references point only to
discovered queue entries, the full assessment, its named metrics, or existing recommendation
entries. Reference type, pointer, queue, provenance, recommendation membership, completeness,
and canonical order are validated. Sources never cross requests or require database reads
to resolve. Adapter-visible copies cannot change ledger contents.

Drafts contain references, recommendation codes, block types, and supported inability
categories. Extra fields such as counts, actions, priorities, timestamps, source payloads,
or free-form operational prose are rejected. The server renders fixed templates using
canonical snapshots and includes **every** triggered recommendation in canonical order.
Empty recommendations explicitly do not guarantee operational health. Overlapping counts
are never summed, and aggregate evidence never identifies tickets or establishes staffing needs.

The strict `offline-agent-v1` response separates `sources` (complete canonical snapshots)
and `evidence` (resolved references) from `explanation` (server-rendered presentation).
It includes `run_id`, `status`, literal `read_only`, `advisory_only`, and
`requires_human_review` flags, `uncertainty`, and a sanitized `error` or null. Statuses are
`answered`, `needs_clarification`, `insufficient_information`, `unsupported`, and `error`.
There is no approval or execution state. Failures retain prior successful sources for audit
but emit no operational evidence or conclusions; an oversized response fails atomically
without truncated snapshots. Sources and resolved evidence intentionally duplicate a bounded
amount of data to keep responses self-contained and traceable. Nothing is persisted.

`tests/test_offline_agent_evaluations.py` contains **offline deterministic evaluations** for
the complete workflows, four simultaneous signals, empty/no-trigger queues, missing and
ambiguous names, authorization, injection-style questions, schema/grounding attacks,
budgets, exceptions, size/deadline limits, sanitization, and read-only/query/session invariants.
They exercise the real dispatcher on both database backends. They establish scripted control
and evidence guarantees; they do **not** measure real-model routing accuracy, hallucination
frequency, general language understanding, or production readiness.

## OpenAI Responses adapter (Sprint 4C)

`openai_responses.OpenAIResponsesAdapter` is an internal, disabled-by-default transport for
`run_agent()`. This milestone authorizes implementation and mocked testing only;
**it does not authorize live model calls**. No agent HTTP endpoint, provider-enabled CLI, provider startup
dependency, hosted tool, framework, write tool, persistence, or deployment is added.

Install the optional official Python SDK and run provider tests without real credentials:

```bash
python -m pip install -e ".[dev,openai]"
env -u TEST_DATABASE_URL python -m pytest -q tests/test_openai_responses.py
```

The `openai` extra requires `openai>=2.54,<3`, verified with 2.54.0. CI installs the extra;
tests use `httpx.MockTransport`, block real HTTP transport, and supply only a test sentinel.
Without the extra, SDK transport tests skip; backend/fake/configuration/import checks remain
available. No provider SDK or client is loaded by application startup or health checks.

`ResponsesSettings` is frozen and strictly typed, constructed only by trusted local code.
It does not load `.env` or enable itself from environment variables:

| Setting | Default | Bounds / source |
|---|---|---|
| `enabled` | `False` | Explicit trusted-code opt-in; live execution still needs approval |
| `model_id` | `None` | Explicit nonempty model ID required when enabled; no built-in model |
| `timeout_seconds` | `10` | Positive, at most 30 seconds; capped by remaining runtime deadline |
| `max_output_tokens` | `2048` | Integer 16–8192, including reasoning tokens |
| `max_requests` | `3` | Integer 1–3 per run; runtime turn limits also apply |
| `max_argument_bytes` | `4096` | Integer 1–4096 UTF-8 bytes before argument parsing |
| `max_payload_bytes` | `196608` | Integer 1024–196608 for request, response, and replay history |

Only an enabled run's first turn reads `OPENAI_API_KEY` from the process environment.
Missing/blank keys fail before client construction. Credentials cannot be set through
settings, questions, or arguments. The client fixes `https://api.openai.com/v1`, ignoring
`OPENAI_BASE_URL`; there is no custom URL setting. Keep keys out of source control,
reports, SDK debug logs, and external request tracing. Any future separately approved live
evaluation must use synthetic operational data only.

Every turn uses `client.responses.create()`, `store=False`, `parallel_tool_calls=False`,
a bounded output-token limit, client `max_retries=0`, and a request timeout reduced to the
remaining monotonic deadline. Only the runtime's currently permitted functions are sent:
`list_queues` or `get_queue_recommendations`, with strict, extra-forbidden schemas.
Raw argument strings pass through Sprint 4A's UTF-8 size and duplicate-key checks before
strict tool validation. Extras and boolean/string/float/nonpositive queue IDs are rejected.
The runtime independently enforces permissions, selection, transitions, and tighter tool
budgets. Invalid/duplicate call IDs, multiple calls, disallowed tools, and ambiguous
call/answer combinations fail closed. No retries or repair loops run.

The runtime opens a private adapter scope per run and closes it on every exit. Stateless
continuation replays accepted provider output items, including encrypted reasoning and
original function calls, then appends `function_call_output` with the matching `call_id`.
Its JSON contains the actual successful tool source snapshot and server-issued reference
metadata. Provider-authored tool results never enter the ledger. No `previous_response_id`
is used. Reusing one configured adapter creates independent clients, histories, budgets,
and call-ID sets. Direct callers must use one `open_run()` context per conversation.

The internal Pydantic draft schema has defaults and optional fields. A minimal provider
`text.format` schema instead requires all seven fields, uses nullable `inability`, and
forbids extras on every object. Conversion rejects duplicate fields, requires the complete
shape, and validates through the existing strict `StructuredAnswerDraft`. The runtime still
checks provenance and complete canonical recommendations in order, then renders fixed
operational text. Schema compliance does not prove correctness. Counts, actions, severity,
evidence payloads, and arbitrary explanatory prose cannot be provided as answer fields.

Refusals map to the existing sanitized refusal. Missing SDK/credentials, authentication,
quota/rate limits, network/request/configuration errors, incomplete/truncated responses,
and malformed/ambiguous output map to `ModelFailure(unavailable)` and runtime
`model_unavailable`. SDK timeouts/exhausted deadlines map to `ModelFailure(timeout)` /
`deadline_exceeded`, preserving existing contracts. Provider exceptions, refusal text,
headers, and transcripts are never returned. SDK serialization warnings that could echo
malformed values are suppressed before explicit validation. HTTP timeouts bound individual
network phases, not the entire process; late responses are rejected and existing cooperative
deadline, database, and pool limitations remain.

Protocol references: official [function calling](https://developers.openai.com/api/docs/guides/function-calling),
[structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), and
[stateless reasoning](https://developers.openai.com/api/docs/guides/reasoning) documentation.
Mocked transport verifies SDK parsing, request construction, continuation, state isolation,
and server grounding. **No live model performance, routing accuracy, availability,
hallucination rate, cost, or production reliability has been verified.**

## Local offline CLI demonstration (Sprint 4D)

**Deterministic simulation, not live-model intelligence.** The CLI runs locally in one
process and reads the existing migrated database using the real runtime and tools. It
needs the backend dependencies and `DATABASE_URL` from your existing local environment
or `.env`. It never creates tables, seeds data, persists assessments, or modifies operational
rows. No OpenAI SDK, API key, provider configuration, or model request is used.

From the repository root, activate the existing virtual environment. A trusted local
operator must set the visible queue scope explicitly in the **process environment**.
Use IDs that operator has authorized in the local database; the following assumes queue
1 is authorized and exists. Queue IDs and names depend on your data, so substitute the
listed ID/name when necessary:

```bash
source .venv/bin/activate
export AGENT_ALLOWED_QUEUE_IDS='1'
# For multiple explicitly authorized queues: export AGENT_ALLOWED_QUEUE_IDS='1,2'
python -m ai_operations_agent.agent_cli queues
python -m ai_operations_agent.agent_cli assess --queue-id 1
python -m ai_operations_agent.agent_cli assess --queue-name 'Technical Support'
python -m ai_operations_agent.agent_cli assess --queue-id 1 --evidence
python -m ai_operations_agent.agent_cli assess --queue-id 1 --json
python -m ai_operations_agent.agent_cli assess --queue-id 1 --question 'Show the configured review suggestions.'
python -m ai_operations_agent.agent_cli --help
```

`AGENT_ALLOWED_QUEUE_IDS` accepts 1–32 distinct positive decimal database IDs, at most
2,147,483,647 each, separated by commas; surrounding whitespace is allowed. Its maximum
size is 512 UTF-8 bytes. Missing/empty values, wildcards, duplicates, malformed IDs, and
oversized lists fail closed before database access. This is trusted local configuration,
not authentication: anyone who controls the process environment controls its queue grants.
No permissions are taken from `.env`, command-line flags, or question text. Unknown IDs
grant no existing data; selecting an unknown or forbidden ID produces the same controlled
`queue unavailable` error. Discovery can return an empty authorized list.

`queues` calls only scoped `list_queues`. Assessment by ID calls
`get_queue_recommendations` once. Assessment by name first discovers authorized queues,
then uses an exact match ignoring case and surrounding whitespace. Missing names fail;
ambiguous names require selection by ID. The CLI never chooses the first match. Arguments
select within scope and cannot expand it. No tool names, SQL, provider URLs, models,
credentials, or permission flags are accepted.

The optional question is demonstration text only. **The CLI does not interpret arbitrary
questions**, route natural-language requests, or answer forecasts/staffing questions.
The explicit command always determines the workflow, regardless of question content.
The entire serialized request, including question, workflow, and optional queue name,
must fit 4,096 UTF-8 bytes; queue names are limited to 160 characters. Common options can
appear before or after the command.

Human-readable assessment output includes queue name/ID, evaluation time, active tickets,
recorded severity, all canonical recommendations in their existing order, uncertainty,
and advisory/human-review status. Empty recommendations mean only that no configured
rule triggered. Untrusted terminal control characters, ANSI sequences, bidi controls,
and Unicode line separators are escaped. `--evidence` shows successful source IDs/tool
names and validated references, **not a complete trace of failed or rejected calls**.

`--json` emits exactly the existing `offline-agent-v1` `AgentResponse`, including sources,
resolved evidence, flags, explanations, and sanitized errors. It preserves source values
using ASCII JSON escapes; all CLI JSON is still deterministic simulation. Evidence is
already included in JSON, so `--evidence --json` adds no extra output. Final text/JSON is
bounded to 196,608 UTF-8 bytes including escaping and the trailing newline. Oversized
output fails atomically. Failed runs emit no operational conclusions/evidence; JSON may
retain prior successful source records under the runtime's existing contract.

Exit codes: `0` answered; `2` invalid arguments or request size; `3` invalid/missing scope
or database/policy configuration; `4` database/runtime/tool/output failure; `5` unknown,
forbidden, or ambiguous queue. Human-readable failures go to stderr; JSON execution
results, including controlled failures, go to stdout. Argument parsing errors always go
to stderr, with no echoed argument values. No raw exception, connection string,
credential, or private provider transcript is printed.

The deterministic adapter only selects permitted tool requests and builds structured
drafts from the runtime conversation's actual snapshots and issued references. It has
no database/dispatcher capability and performs no independent assessment or recommendation
evaluation. The runtime validates grounding and renders the final canonical text. Existing
policy, transaction consistency, cooperative timeout, and local-only limitations apply.
Nothing here demonstrates real-model language understanding or production readiness.

## Tests and quality checks

```bash
env -u TEST_DATABASE_URL .venv/bin/python -m pytest -q  # in-memory SQLite
.venv/bin/python -m ruff check .
git diff --check
```

Run the **same API, Feature 001–003, Sprint 4A, offline Sprint 4B, mocked Sprint 4C, and local offline Sprint 4D suites against migrated PostgreSQL 16**. The `db_test`
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
tables; Alembic owns the schema. The Sprint 4A fixtures additionally commit only their own
uniquely named rows in this dedicated test database so independent read-only transactions can observe them, then delete exactly those
rows and compare all business rows to the pre-test baseline. They never reconfigure existing
write-fixture transactions. Run one test process at a time against this database.
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
