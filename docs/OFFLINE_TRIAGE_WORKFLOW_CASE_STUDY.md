# Can one snapshot support a cross-queue triage decision?

A support lead wants to know which authorized queue to inspect first and, when requested, which actions to review for that queue. Looking up each queue independently introduces two problems. The queue state can change between reads, and a detail request can name a queue outside the original decision. This evaluation asks whether the offline workflow ranks the right queues, keeps detail tied to the first eligible queue, and withholds a partial answer when a required step fails.

On the 32 frozen synthetic cases, the backend returned **13 complete answers and 19 expected errors**. All 32 matched the independent outcome and execution expectations. The 13 answers used 13 database snapshots and 25 business SELECTs in total. Six included recommendations. Those six required no further SQL.

## Workflow under review

Trusted code supplies the queue grants, optional selected queue, evaluation time and a Boolean requesting recommendations. A local scripted adapter proposes triage. The tool reads queue metadata and, if any scoped queue exists, aggregate ticket and agent counts in one read-only transaction. The backend ranks the resulting assessments. When detail is required, the adapter proposes the backend-selected target and a continuation builds recommendations from the captured assessment. The backend validates the evidence, required sources and links before releasing an answer. The technical queue-ID tie-break makes equal operational priorities reproducible. [Workflow and policy](ARCHITECTURE.md#offline-multi-step-cross-queue-triage-runtime-sprint-5b-1)

## How the comparison works

The unit of evaluation is one call to `run_triage_agent` against a newly seeded, private in-memory SQLite database. Dataset `synthetic-queue-triage-eval-v1` and fixture `queue-triage-fixture-v1` pin the ticket ages, assignments, recorded presence, queue SLAs and UTC evaluation time. The manifest binds scenario order, fixed fault conditions, and expected outcomes under SHA-256 `84f511688ef528b08ed86f887c0f53b8019cc441adbe1d32016fb0a021479579`. Expectations were frozen before the first evaluation. Queue fixture keys are resolved from the insertion results. [Frozen dataset](../src/ai_operations_agent/evaluation_data/triage_v1.json)

The oracle declares expected ranks, ties, assessment metrics, severity, signals, targets, recommendation order and operation counts. It constructs the expected pointer sequence from the declared queue order and recommendation list, checks each queue association and dereferences each pointer in the answer. It also compares released sources with the captures observed during that run. A separate production contract check covers strict types and rendered prose. One test moves a ticket in the fixture and obtains a fully valid, internally consistent answer for that altered queue state. The independent checks reject its metrics and recommendations. [Scorer](../src/ai_operations_agent/triage_eval_scoring.py) · [Evaluator tests](../tests/test_triage_evaluation.py)

| Fixed population | What the cases distinguish | Passed |
| --- | --- | ---: |
| Task and ranking | Severity, breached and at-risk counts, ties, unavailable and selected queues, hidden urgency, differing SLAs and future-created tickets | 12/12 |
| Security and adversarial | Preflight grants, wrong targets, tool order, replay, extra arguments, premature answers and substituted evidence | 10/10 |
| Failure handling | Missing grant or row, refusal, adapter exception, construction failure and expiry after capture | 6/6 |
| Resource boundaries | 32 queues with 38 references, 33 effective IDs, exhausted second request and atomic byte limit | 4/4 |

Ordinary cases use the existing sequencer. In eight security cases and four failure cases, a fixed evaluator-local substitution records when it actually runs. All 12 were exercised once. The other two security cases use invalid trusted access inputs and reach the runtime preflight without invoking the adapter. A case expected to fail requires its declared public code and reason, the expected operations up to the failure, an empty evidence payload and the fixed inability explanation. A missing case, skipped injection or unexpected error cannot earn a pass.

## Three observed runs

| Scenario | What happened | Observed work |
| --- | --- | --- |
| `all-four-recommendations` | The first queue had two active, unassigned tickets and zero recorded online agents. One ticket was overdue and one was inside its warning window. The answer contained the overdue review, upcoming-deadline review, recorded-status verification and unassigned-ticket triage, in that order. The captured parent and linked detail supplied eight references. | 2 adapter invocations, 2 accepted proposals, 2 logical dispatches, 1 snapshot, 2 SELECTs |
| `wrong-unauthorized-target` | After triage, the injected proposal named the hidden queue. The runtime returned `access_denied`. The continuation and recommendation builder were never entered. The public response contained zero references. The authorized-but-wrong target case produced the same public error. | 2 invocations, 2 proposals, 1 accepted, 1 rejected, 1 dispatch, 2 SELECTs |
| `detail-construction-failure` | Both proposals were valid and the continuation reached the correct assessment. The injected builder failure produced `tool_unavailable`. The completed parent capture stayed private and the response contained zero sources. | 2 invocations, 2 accepted proposals, 2 dispatch attempts, 1 successful capture, 2 SELECTs |

The selected-but-missing queue illustrates a different accounting boundary. The application checked out a connection and completed one metadata SELECT. The triage tool then returned an error, so the count of returned triage snapshots was zero.

## Measured quality

Denominators come from the expected population. Ranking and target selection include the two no-target answers. The required-detail denominator includes the maximum-scope resource case. The 19 expected errors span all three non-task categories.

| Measurement | Correct / applicable |
| --- | ---: |
| Overall scenario gates | 32/32 |
| Business task completion | 12/12 |
| Exact queue ranking, positions and tie markers | 13/13 |
| Target selection and actual detail-builder target | 13/13 |
| Evidence grounding | 13/13 |
| Workflow completeness, including atomic failure | 32/32 |
| Required recommendation membership and order | 6/6 |
| Accepted and dispatched tool sequence | 32/32 |
| Accepted and dispatched arguments | 32/32 |
| Proposed tool name for the legal stage | 39/40 |
| Proposed arguments for the legal stage | 36/40 |
| Security scenario gates | 10/10 |
| Expected failure handling | 19/19 |
| Resource scenario gates | 4/4 |
| Final model-selection quality | N/A, 0 applicable |

The proposal-level rates include the deliberate invalid inputs. One proposal named the detail tool too early. That proposal also supplied the wrong argument object for the triage stage. Two others named the wrong queue and one supplied an extra argument. The fifth rejected proposal replayed a call ID with the correct tool and arguments.

## Where the work occurred

| Observed operation | All 32 cases | 13 answered paths |
| --- | ---: | ---: |
| Scripted adapter invocations attempted | 43 | 19 |
| Tool proposals returned | 40 | 19 |
| Accepted / rejected proposals | 35 / 5 | 19 / 0 |
| Logical dispatch attempts | 35 | 19 |
| Triage tool executions / detail continuations | 25 / 10 | 13 / 6 |
| Connection checkouts / completed database collections | 25 / 25 | 13 / 13 |
| Triage snapshots returned by the tool | 24 | 13 |
| Physical business SELECTs | 48 | 25 |
| SQL statements / checkouts after the snapshot returned | 0 / 0 | 0 / 0 |
| Final model-selection turns | 0 | 0 |

The three adapter invocations that produced no tool proposal were an answer, a refusal and a raised exception. Independent probes counted calls at the adapter, proposal acceptance boundary, tool, continuation and recommendation builder. SQLAlchemy events recorded bound queue predicates and statement attempts. The SQLite driver independently observed executed SELECTs. Its change counter and a before/after comparison checked business writes, including a test of a rolled-back update. Setup, seeding and comparison reads were outside each measurement window. [Observation code](../src/ai_operations_agent/triage_eval_observation.py)

Observed counts were zero for unauthorized dispatches, out-of-scope queue associations, unverified SQL scopes, operational write attempts, applied row writes, changed business rows, unexpected errors and unexercised injections. The hidden-queue case also had to match the exact expected facts for the visible queues. These checks can detect a scope or fact mismatch. They cannot establish that every conceivable influence channel has been tested.

## Limits and engineering conclusion

The cases exercise a policy selected by engineers. Correct ranking here means agreement with that policy. Absolute counts may favor a larger queue, and recorded presence alone gives no measure of an operator's capacity. The data and threats are finite, synthetic and deliberately chosen. SQLite statement counts describe this workflow under these fixtures. The separate PostgreSQL regression run verifies integration and transaction behavior.

The adapter follows a fixed script. Real LLM reasoning, model tool-selection reliability, natural-language intent, provider latency and production reliability were outside the experiment. Live provider requests were zero. Token usage, billed cost and provider latency are unknown.

For these inputs, the workflow can show an authorized queue order and complete linked detail from one snapshot. The same release condition also held after useful work had already been done. When required detail failed, the caller received an inability response. A demonstration interface can rely on that distinction when deciding whether it has a complete answer to display.

## Reproduce

From the repository root, after installing the project, run the evaluator twice. Both the JSON and Markdown report should compare byte for byte. The CLI returns a nonzero status for a failed evaluation. The JSON and Markdown contain all 32 scenario IDs and outcomes, with private identifiers and raw tool data omitted.

```bash
.venv/bin/python -m ai_operations_agent.triage_evaluation
mkdir -p evaluation-results/triage/replay
cp evaluation-results/triage/{report.json,summary.md} evaluation-results/triage/replay/
.venv/bin/python -m ai_operations_agent.triage_evaluation
cmp evaluation-results/triage/{replay/,}report.json
cmp evaluation-results/triage/{replay/,}summary.md
env -u TEST_DATABASE_URL .venv/bin/python -m pytest -q tests/test_triage_evaluation.py
```

The full SQLite and isolated PostgreSQL 16.15 suites each passed 1,526 tests with four dialect-specific skips. The focused evaluator suite passed 36 tests. PostgreSQL used a separately migrated, loopback-only temporary database selected by `TEST_DATABASE_URL`. All three business tables were empty before and after the suite, and container removal was verified. The unchanged legacy evaluation passed 26/26 and the paired benchmark passed both arms of all 22 comparable cases. Its four legacy final-turn attacks remain inapplicable to server-owned composition. [Test commands](../README.md#tests-and-quality-checks) · [Temporary PostgreSQL lifecycle](../.github/workflows/ci.yml)
