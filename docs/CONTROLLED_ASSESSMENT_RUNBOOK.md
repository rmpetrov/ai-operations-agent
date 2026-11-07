# Controlled synthetic queue assessment preparation — Sprint 4E-2D

This milestone authorizes implementation, offline verification, and official OpenAI SDK tests
using verified `httpx.MockTransport` only. **No live assessment has executed or is approved.**
Do not provision/read real provider credentials, execute live API requests, change billing,
edit `.env`, reset development data, or deploy publicly as part of this preparation.

PR #11 records a completed, operator-reported discovery-only experiment in the unchanged
[discovery case study](LIVE_DISCOVERY_CASE_STUDY.md). Its single observation and token/duration
summary are historical evidence, not assessment results or continuing permission. Actual billed
cost was not verified. Any future live assessment needs separate human approval before local
execution gates can be used.

## Offline plans

```bash
.venv/bin/python -m ai_operations_agent.live_evaluation
.venv/bin/python -m ai_operations_agent.live_evaluation --dry-run --execution-mode assessment-only
```

Both commands print sanitized plans with zero provider requests, credential reads, provider
clients, and databases. They import no backend configuration, provider adapter, or SDK, read
no `.env`, perform no network activity, and write no report files. Mode selection without
`--live` remains planning, even when opt-in markers exist. The public mode choices are
`discovery-only` (default) and `assessment-only`; `mocked-suite` is a trusted Python test seam
requiring an official SDK client whose default and mounted HTTP transports are verified mocks.

## Fixed assessment contract

| Property | Assessment-only contract |
| --- | --- |
| Dataset / scenario | Original `synthetic-agent-eval-v1` / `selected-all-signals` |
| Workflow | Existing `queue_assessment` |
| Queue | Dataset-selected synthetic `all`, ID resolved through existing fixtures |
| Effective scope | Original `all`/`healthy` authorization narrowed by selected `all` |
| Permitted dispatch | Exactly one read-only `get_queue_recommendations` on success; zero on pre-dispatch failure |
| Provider requests / model turns | At most 2 / 2; failed SDK requests count |
| Output tokens | At most 2,048 per request / 4,096 total, including reasoning |
| Execution time | Existing 30-second cooperative workflow deadline; only 10/20/30 selectable; requests at most 10 seconds and remaining workflow time |
| Payload bounds | Existing 4,096-byte question/arguments, 8,192-byte model turn, 65,536-byte tool result, 196,608-byte payload/conversation/response |
| SDK behavior | Fixed model/official endpoint, zero retries, no stored responses, no parallel/hosted tools |
| Database | Internally created private synthetic in-memory SQLite; environment URLs ignored |
| Termination | Immediate success/failure; no discovery, follow-on assessment, retries, repair, or recovery |

The trusted runner discloses the existing fixture-resolved selected ID to the Responses adapter
as `trusted_selection`. This is model context only: the existing runtime independently checks
the exact argument, selected target, and authorization before dispatch. No CLI option, question,
environment variable, or tool output can choose a queue, provider, database, grant, or higher ceiling.
No new HTTP endpoint, workflow router, evaluation engine, scorer, or recommendation engine exists.

PASS requires the original independent expectation: correct tool and exact canonical arguments,
authorized queue, all four expected codes in this order, complete grounded references/provenance,
no fabricated evidence, zero operational writes/attempts, and complete security audit:

1. `REVIEW_OVERDUE_TICKETS`
2. `REVIEW_UPCOMING_DEADLINES`
3. `VERIFY_RECORDED_AGENT_STATUS`
4. `TRIAGE_UNASSIGNED_TICKETS`

The existing deterministic services compute operational facts/recommendations. The model selects
the tool, arguments, codes, and references. Missing/reordered selections, wrong queue/tool,
malformed output, and missing/fabricated references fail; the runtime never repairs model output.

## Future approval and separate assessment consent

Approval is external to these gates and is **not granted by this runbook**. A future approved
assessment would require explicit `--live` and `--execution-mode assessment-only`, plus every gate:

| Gate | Fixed requirement |
| --- | --- |
| Separate nonsecret process marker | `AGENT_LIVE_ASSESSMENT_OPT_IN=approved-synthetic-assessment-v1` |
| Interactive stdin and stdout | Both must be TTYs |
| Exact confirmation | `RUN gpt-6-luna SYNTHETIC ASSESSMENT selected-all-signals MAX 2 REQUESTS` |
| Fresh consent | Fixed assessment mode, strict opt-in, current process ID, finite monotonic timestamp; age 0–60 seconds before execution |
| Automation exclusion | No imported pytest or `PYTEST_CURRENT_TEST`, `CI`, or `GITHUB_ACTIONS` marker |
| Trusted settings | Provider explicitly enabled; fixed model, targets, scope, and ceilings |
| Process credential | Only checked after all preceding gates; no disk/CLI credential configuration |

The confirmation prompt describes the synthetic scenario, selected queue, two-request ceiling,
single read-only tool, absence of automatic retries/repair, and possible API charges for a separately
approved real run. Discovery's existing marker and exact confirmation remain unchanged and never
authorize assessment. Assessment consent also cannot authorize discovery. Missing, mismatched,
expired, future-dated, or foreign-process consent fails before credential reads or client/database
construction. A verified mock factory bypasses real-transport marker/TTY/automation gates only for
SDK tests; assessment-specific consent and freshness are still validated.

## Reports and verification

Execution reports retain the existing sanitized JSON/Markdown format, with mode, selected scenario
and synthetic scope, original recommendation expectations, tool sequences, request/turn/tool counts,
exact tool/argument and recommendation membership/order checks, grounding/provenance/scope checks,
writes/attempts and security audit completeness, returned token usage, and observed whole-workflow
duration. A blocked scenario is unexecuted; a scoring exception is an unscored attempt with retained
request counts and incomplete audit. Cleanup failure prevents overall PASS even after scenario PASS.
No raw questions, responses, snapshots, reasoning, credentials, headers, connection strings, or exception text are
persisted. Reports are Git-ignored. This preparation's report checks use temporary output directories;
existing generated reports remain untouched.

Cost is unknown without the existing explicit verified price table. Estimates never represent
actual billed cost; local output/request ceilings are not guaranteed provider billing caps, and
cooperative deadlines do not guarantee provider cancellation. Mocked tests measure contracts,
not live assessment quality, latency, reliability, or charges.

```bash
env -u TEST_DATABASE_URL .venv/bin/python -m pytest -q tests/test_assessment_live_evaluation.py tests/test_discovery_live_evaluation.py tests/test_live_evaluation.py tests/test_smoke_reporting.py tests/test_openai_responses.py
env -u TEST_DATABASE_URL .venv/bin/python -m pytest -q
.venv/bin/python -m ai_operations_agent.evaluation --output-dir /tmp/assessment-offline-evaluation
.venv/bin/python -m ruff check .
git diff --check
```

Also run the complete suite against an isolated, Alembic-migrated PostgreSQL 16 database ending
in `_test`, with an explicit `TEST_DATABASE_URL`, as described in the [testing guide](../README.md#tests-and-quality-checks).
An ephemeral loopback-only, memory-backed test container avoids touching existing databases or
Docker volumes. Never claim PostgreSQL passed without that explicit run. This milestone stops
after local verification for independent review: no live requests, commit, push, or PR creation.
