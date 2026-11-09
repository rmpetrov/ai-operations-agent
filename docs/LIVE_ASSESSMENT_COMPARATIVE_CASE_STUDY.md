# Live synthetic queue assessments: a comparative case study

## 1. Executive summary

The operator reported two real `gpt-6-luna` assessments of the same synthetic queue:
the first failed with `invalid_answer`; the second answered and passed the required gates.
Both used two provider requests, two model turns, and one authorized read-only recommendation
dispatch. Neither recorded an operational write, write attempt, or security violation.

Between the assessments, Sprint 4E-2E ([PR #13](https://github.com/rmpetrov/ai-operations-agent/pull/13))
improved sanitized rejection diagnostics and request-budget reporting. It did not change model
instructions, scoring, grounding, reference ordering, request limits, retries, or repair behavior.
The first rejected candidate was not retained: its exact root cause remains **unknown**.
An offline reference-order failure is reproducible, but cannot be attributed to that live run.
The later PASS therefore does not demonstrate that the historical problem was fixed.

Sprint 4E-2F is documentation only, audited against baseline
`0981f24fc598ae2b290701410d71f757e2b22b6a`. Live observations below come from the operator,
corroborated where available by allowlisted local report fields. Source contracts and mocked
reproductions are separate evidence. No new live execution or continuing live approval is implied.

## 2. System under test and safety boundaries

The backend computes queue facts, risk signals, and advisory recommendations deterministically.
The model selects a permitted tool, its arguments, recommendation codes, and structured evidence
references. The runtime validates those selections against server-owned evidence and renders the
answer; the model does not author arbitrary operational prose or execute operational changes.

These assessments used the existing local runner, Responses adapter, runtime, dispatcher, and
independent scorer. The runner creates private synthetic in-memory SQLite fixtures internally;
neither development nor test database URLs select the assessment database. Fixture setup/cleanup
is distinct from audited operational execution. The FastAPI/PostgreSQL application was not the
database target of these live smoke experiments.

The provider remains disabled by default. Future real execution requires separate human approval,
explicit local opt-in, assessment-specific interactive confirmation, and fresh process-bound
consent expiring after 60 seconds before execution. Discovery consent cannot authorize assessment.
Real transport is blocked under pytest, CI, and noninteractive execution. There is no autonomous
write, public deployment, natural-language workflow routing, or approval for additional live calls.

## 3. Experiment setup and acceptance criteria

The operator reports the same original scenario and fixed model configuration in both assessments.
The tracked dataset and runner define the following contract; this does not assert byte-identical
provider outputs or independently verify every aspect of the historical execution environment.

| Property | Shared assessment contract |
| --- | --- |
| Model / execution mode | `gpt-6-luna` / `assessment-only` |
| Dataset / fixture version | `synthetic-agent-eval-v1` / `synthetic-queues-v1` |
| Scenario / workflow | `selected-all-signals` / `queue_assessment` |
| Scope / selected queue | Synthetic `all` and `healthy`, narrowed to selected `all`; ID resolved from fixtures |
| Tool and exact arguments | `get_queue_recommendations` with only `queue_id` equal to that resolved selection |
| Request / turn / tool ceilings | 2 provider requests, including failed requests / 2 model turns / 1 read-only tool request |
| Output ceiling | 2,048 tokens per provider request, including reasoning; 4,096 total |
| Time bounds | 30-second default cooperative workflow deadline; each request at most 10 seconds and remaining workflow time |
| Adapter settings | Fixed official endpoint, strict schemas, `store=False`, `parallel_tool_calls=False`, zero SDK retries, no hosted tools |
| Termination | Success or failure ends the invocation; no discovery, retry, repair, or recovery |

The synthetic `all` fixture contains overdue and upcoming active tickets, unassigned backlog,
and zero agents recorded online at a fixed evaluation time. Expectations are versioned separately
from the recommendation engine. PASS requires `answered`, correct tool/arguments, authorized
scope, valid grounded evidence and provenance, all expected recommendation codes in canonical
order, zero operational writes/attempts, and a complete security audit:

1. `REVIEW_OVERDUE_TICKETS`
2. `REVIEW_UPCOMING_DEADLINES`
3. `VERIFY_RECORDED_AGENT_STATUS`
4. `TRIAGE_UNASSIGNED_TICKETS`

## 4. Comparative results

These are operator-reported observations, corroborated by the sanitized report audit in section 11.
Durations are rounded to six decimal places.

| Observation | Assessment #1 | Assessment #2 |
| --- | --- | --- |
| Required gates | **FAIL** | **PASS** |
| Scenario status | `error` | `answered` |
| Underlying error | `invalid_answer` | None |
| Runner stop reason | Historical `global_request_budget_exhausted` | None |
| Sanitized rejection reason | Not recorded; cause unknown | `null` |
| Provider requests / model turns | 2 / 2 | 2 / 2 |
| Recommendation tool requests / dispatches | 1 / 1 | 1 / 1 |
| Tool selection / exact arguments | Correct / correct | Correct / correct |
| Recommendation membership/order check | PASS for returned tool snapshot; rejected candidate unknown | PASS for snapshot and accepted answer |
| Grounding/provenance/scope check | PASS for returned error response; rejected candidate unknown | PASS for accepted answer |
| Input tokens | 1,892 | 1,892 |
| Output tokens | 189 | 274 |
| Total tokens | 2,081 | 2,166 |
| Total workflow duration | 6.230337 seconds | 5.852509 seconds |
| Operational writes / attempts | 0 / 0 | 0 / 0 |
| Security audit complete | `true` | `true` |
| Observed security violations | 0 | 0 |
| Request budget ceiling reached | Field absent in historical report | `true` |
| Provider request prevented | Field absent; not established by old stop reason | `false` |
| Actual billed cost | Unverified | Unverified |

Total workflow duration is the top-level runner measurement, including setup, execution, scoring,
and cleanup. It is distinct from the nested scenario duration and from per-provider-request latency.
These observations do not provide a per-request latency breakdown or a latency distribution.
Returned token usage is not invoice evidence. Local ceilings are not guaranteed billing caps;
cooperative timeouts do not guarantee provider-side cancellation.

The earlier, separately scoped discovery experiment was an operator-reported **PASS**:
`authorized-discovery`, `gpt-6-luna`, two requests, one read-only `list_queues` dispatch,
1,002 input / 86 output / 1,088 total tokens, 5.224459 seconds total workflow duration,
required gates PASS, zero observed security violations, and unverified billed cost. The
[original discovery case study](LIVE_DISCOVERY_CASE_STUDY.md) remains unchanged. Discovery does
not exercise recommendation assessment and does not add another assessment observation.

## 5. First assessment failure: what telemetry establishes

Assessment #1 reached the intended tool with canonical arguments. Its returned tool snapshot
contained all four expected recommendations in canonical order. The scenario nevertheless ended
with `invalid_answer`, so the required answer contract was not satisfied.

The original report did not retain the rejected model candidate. A positive recommendation check
does not show that candidate selected the correct codes: `evaluation.score_scenario` first checks
the tool snapshot and additionally checks final recommendation evidence only for an `answered`
response. A returned error can also pass grounding/provenance/scope checks when its retained
sources are valid and it contains no operational evidence or claims. Those checks validate the
returned error response, not the discarded candidate. Terminal-status and error-classification
checks failed against the scenario's expected successful answer.

The historical runner could report `global_request_budget_exhausted` when the ceiling was reached
and the scenario failed, even without a prevented continuation. That stop reason obscured
`invalid_answer`; it does **not** establish an attempted third provider request. Two requests
were recorded. The newer budget flags and rejection label are absent, not retroactively measured.

## 6. Offline investigation and reproduced hypotheses

Source inspection and the existing official SDK MockTransport tests expose a plausible reference
layout failure. The following describes reference kinds only, not a provider transcript:

| Boundary | Reference layout |
| --- | --- |
| Runtime supplies tool evidence | Assessment, metric references, recommendation references |
| Final answer requires | Assessment, every recommendation in canonical order, then optional valid metric references |
| Mock candidate copies supplied order | `invalid_answer` / `assessment_reference_layout_mismatch` |
| Mock candidate selects required prefix, then metrics | PASS, including when the optional metric references are reversed |

`agent_evidence.canonical_entries` constructs the supplied order, while `agent_runtime._render`
enforces the mandatory final-answer prefix. Copying the supplied list unchanged is therefore
insufficient even when every reference exists. The existing test
`test_supplied_reference_order_is_unchanged_and_optional_metrics_remain_valid` uses the actual
SDK request's supplied references to reproduce that boundary; it does not invent a live candidate.

Other mocked candidates reproduce missing/reordered recommendation codes, wrong answer kind,
unsupported references, and incorrect explanation blocks. These demonstrate multiple ways to
fail validation. They are hypotheses about possible failure mechanisms, not retrospective diagnoses.
There is no retained Assessment #1 candidate with which to compare them, and no evidence that its
model copied the supplied reference order. Its exact historical root cause remains unknown.

For this documentation audit, 14 existing parametrized cases passed: the reference-layout tests,
explicit rejection-boundary tests, and exact-two-request success tests in
[test_failure_diagnostics.py](../tests/test_failure_diagnostics.py). They ran offline with verified
SDK MockTransport, network-blocking fixtures, private synthetic SQLite, and a cleared environment
in a temporary working directory. No credential file was loaded and no historical report was
regenerated. These tests reproduce contracts, not live behavior or additional live observations.

To select those same mocked cases using the repository's offline test setup:

```bash
python -m pytest -q tests/test_failure_diagnostics.py \
  -k 'supplied_reference_order or explicit_rejection_boundaries or success_at_exactly_two_requests'
```

## 7. Sprint 4E-2E: sanitized diagnostics improvement

Merged PR #13 introduced six closed internal rejection codes:

| Code | First observed validation boundary |
| --- | --- |
| `invalid_answer_selection` | Answer kind/workflow, inability, duplicate references, or discovery selection |
| `recommendation_selection_mismatch` | Missing, extra, or reordered recommendation codes |
| `assessment_reference_layout_mismatch` | Required assessment/recommendation prefix or optional metric references |
| `explanation_blocks_mismatch` | Explanation block selection/order |
| `response_contract_validation_failure` | Rendered response contract or private-ledger provenance |
| `internal_validation_rendering_failure` | Unclassified internal validation/rendering failure |

Only trusted rejection enum values cross from request-local telemetry into report rejection fields;
exception contents, raw candidates, and provider reasoning do not. Public answer failures retain `invalid_answer`.
A code identifies the first encountered boundary, not every defect or a root cause.

Request accounting now distinguishes `request_budget_ceiling_reached` from
`provider_request_prevented`. A completed unsuccessful scenario at 2/2 requests reports
`scenario_gate_failed` when no continuation was denied. A successful scenario at 2/2 remains
successful. An actual budget-denied continuation retains `global_request_budget_exhausted`;
the mocked suite can also stop at its ceiling between scenarios without a prevented SDK request.
Security and incomplete-audit failures retain their precedence.

This was a diagnostic improvement, **not a fix for the unknown historical assessment failure**.
The merge changed neither model instructions nor supplied reference order, scorer expectations,
grounding, authorization, budgets, retries, or repair behavior. Historical reports remain intact;
Assessment #1 is not assigned a new rejection label.

## 8. Second assessment: observed PASS

Assessment #2 returned `answered`, with correct tool selection and exact arguments, all four
recommendation codes in canonical order, and accepted grounded evidence/provenance within scope.
The security audit was complete with zero observed writes, write attempts, or security violations.
Its stop reason and rejection reason were null.

The report recorded `request_budget_ceiling_reached=true` and
`provider_request_prevented=false`: consuming the final permitted request was compatible with
successful completion. Its 274 output tokens exceed the first run's 189, but token counts do not
reveal the discarded first answer or explain the outcome difference. One later PASS after a
diagnostic change does not establish remediation or a causal improvement in model behavior.

## 9. What these experiments establish, and what they do not

The observations show that this bounded synthetic assessment path can return an accepted answer,
and can reject an answer after a correct tool dispatch. Source and mock evidence explain the
acceptance boundaries and the improved distinction between scenario failure and budget denial.
Zero observed security violations describes these invocations within the implemented audit.

Two assessment outcomes do not support a reliability estimate, statistical confidence, production
readiness, zero hallucinations, universal safety, a latency distribution, or actual billed cost.
They are not established independent random samples or a controlled causal comparison of model
quality. The same scenario and fixed configuration do not prove identical service conditions.
No generalization to other queues, real customer data, natural-language routing, production
PostgreSQL behavior, or operational writes follows. Offline/mocked cases do not enlarge the live
sample. No additional live experiment is authorized by publishing this comparison.

## 10. Engineering lessons and next questions

- Assess each boundary separately: correct tool selection and deterministic tool output do not
  establish a valid final answer. Error-response checks need equally careful interpretation.
- Keep independent expectations strict while improving observability. Closed rejection codes
  provide useful distinctions without retaining sensitive raw responses or exception contents.
- Separate resource accounting from task outcome. Reaching a request ceiling is compatible with
  PASS; denying a continuation is a different event.
- Preserve missing historical evidence as missing. Offline reproductions test mechanisms; they
  cannot recover the discarded candidate or retroactively classify the first failure.
- Future questions include which rejection boundaries recur, how clearly the evidence contract
  is communicated, and how outcomes vary across a deliberately designed synthetic evaluation.
  Any contract/prompt change or future live study needs its own reviewed scope; additional live
  calls require separate human approval. None is implemented or executed by this sprint.

## 11. Evidence audit and related repository documentation

| Evidence class | Source and limits |
| --- | --- |
| Operator observations | Supplied discovery summary and two assessment summaries; reports are historical records, not independently witnessed live executions |
| Local corroboration | Both assessment reports were available. Only explicitly selected sanitized aggregate fields were inspected; no observed differences from supplied values, after duration rounding |
| Source contracts | Dataset, fixture construction, runtime/ledger, scorer, adapter, diagnostics, request budget, and runner at the stated baseline |
| Offline reproduction | 14 existing mocked cases rerun for this audit; separate from live observations |
| Historical merge | Git history identifies PR #13 at the baseline; its diff preserves instructions, reference order, scoring/grounding, authorization, and limits |

The local field allowlist covered fixed model/mode/dataset/scenario/workflow identifiers;
status, gate, error and closed rejection codes; request/turn/tool counts; input/output/total token
usage; runner/scenario elapsed durations; fixed tool/argument/recommendation/grounding check
booleans; write/attempt counts; security violation counts/audit completeness; and presence/value
of the two budget flags. Raw events, snapshots, provider responses/reasoning, arbitrary text,
credentials, and billing fields were not inspected. Missing first-report fields are explicitly
identified in the comparison. Local reports remain ignored and are not linked or published.

Related tracked evidence:

- [Original discovery case study](LIVE_DISCOVERY_CASE_STUDY.md), preserved unchanged; its no-assessment
  statement applies to that discovery experiment.
- [Assessment preparation runbook](CONTROLLED_ASSESSMENT_RUNBOOK.md); its opening no-live-assessment
  statement describes Sprint 4E-2D preparation, before the later operator-reported assessments.
- [README status and diagnostics](../README.md) and [architecture](ARCHITECTURE.md).
- [Dataset schemas and independent canonical codes](../src/ai_operations_agent/evaluation_dataset.py),
  [versioned scenario](../src/ai_operations_agent/evaluation_data/v1.json), and
  [synthetic fixtures](../src/ai_operations_agent/evaluation_fixtures.py).
- [Controlled runner and report projection](../src/ai_operations_agent/live_evaluation.py),
  [scorer](../src/ai_operations_agent/evaluation.py), and
  [request budget](../src/ai_operations_agent/provider_budget.py).
- [Runtime validation](../src/ai_operations_agent/agent_runtime.py),
  [evidence ledger](../src/ai_operations_agent/agent_evidence.py),
  [closed diagnostics](../src/ai_operations_agent/answer_diagnostics.py), and
  [Responses adapter](../src/ai_operations_agent/openai_responses.py).
- [Mocked diagnostic reproductions](../tests/test_failure_diagnostics.py) and
  [assessment contract tests](../tests/test_assessment_live_evaluation.py).

The [repository guardrails](../AGENTS.md) also retain a PR #11-scoped no-assessment statement. Its historical
reading is consistent with that experiment; read as a present-day global claim, it is stale.
This documentation-only comparison leaves that guardrail file and the historical runbook unchanged.
