# Controlled synthetic queue assessments: repeatability results and failure analysis

## 1. Executive summary

Five operator-reported live synthetic assessments requested `gpt-6-luna` on the fixed
`selected-all-signals` scenario: **two PASS outcomes, two final-answer validation failures,
and one provider/adapter failure**. The three later repeatability attempts contain one of each.
Correct tool use preceded both final-answer failures. Successful dispatch alone did not establish
successful assessment completion.

The newer rejected answer records `invalid_answer_selection`, identifying an early runtime
selection guard. Its specific rejected contents are unknown. The first historical answer's
rejection reason was not recorded and remains unclassified. The provider/adapter failure records
`model_unavailable`; its root cause and token usage are unknown. A suspected credential problem
is an unconfirmed operator hypothesis.

Sprint 4E-2G performs offline analysis and documentation only, starting from clean, synchronized
main at `a4359b65d08145ee8a3f1f731f942ce305c14244`. All five immutable local reports were available;
allowlisted fields corroborated the supplied observations. Existing offline tests passed, including
official SDK MockTransport reproductions and a valid control. No live requests were performed.
This small observational series demonstrates neither production reliability nor general model
quality. Further live execution requires separate human approval.

## 2. Research question and scope

Can the same guarded synthetic assessment complete repeatedly, and which failure boundaries can
the retained sanitized diagnostics distinguish when it does not?

Historical assessments H1 and H2 are preserved in the
[comparative case study](LIVE_ASSESSMENT_COMPARATIVE_CASE_STUDY.md). R1, R2, and R3 are the three
later manual repeatability attempts. The operator reports no intentional changes to prompts,
scoring, grounding, budgets, or runtime behavior between R1–R3. Their reports identify the same
clean Git revision. This is evidence of a shared tracked baseline, not a complete environment
capture or proof of identical provider conditions. A fixed requested model ID does not establish
immutable provider model weights.

The earlier [discovery PASS](LIVE_DISCOVERY_CASE_STUDY.md) exercises `authorized-discovery` and
`list_queues`. It is separate from these five assessments and is excluded from assessment counts.
Historical reports and operator observations are not independently witnessed live executions;
source inspection and mocked verification provide different kinds of evidence.

## 3. Experiment setup and safeguards

| Property | Fixed assessment contract |
| --- | --- |
| Requested model / mode | `gpt-6-luna` / `assessment-only` |
| Dataset / fixture version | `synthetic-agent-eval-v1` / `synthetic-queues-v1` |
| Scenario / workflow | `selected-all-signals` / `queue_assessment` |
| Authorized selection | Synthetic scope `all` and `healthy`, narrowed to fixture-resolved `all` |
| Permitted dispatch | One read-only `get_queue_recommendations`, with only the selected `queue_id` |
| Request / turn / tool ceilings | 2 provider requests, including failed requests / 2 model turns / 1 tool request |
| Output ceiling | 2,048 tokens per request, including reasoning; 4,096 total |
| Time bounds | Default 30-second cooperative workflow deadline; each request at most 10 seconds and remaining workflow time |
| Transport | Fixed official endpoint, strict schemas, `store=False`, `parallel_tool_calls=False`, zero SDK retries, no hosted tools |
| Termination | Success or failure ends the invocation; no discovery continuation, retries, repair, or fallback |

The runner constructs private synthetic in-memory SQLite fixtures with fixed evaluation time and
policy. Fixture creation/cleanup is separate from audited operational execution. Existing databases
are not assessment targets. The backend computes facts and recommendations; the model selects
structured codes, references, and explanation blocks. The runtime validates and renders the answer.

PASS requires `answered`, correct tool and exact arguments, the complete canonical recommendation
catalog in order, grounded evidence and provenance within scope, zero operational writes/attempts,
and a complete security audit. The independently versioned expected catalog is:

1. `REVIEW_OVERDUE_TICKETS`
2. `REVIEW_UPCOMING_DEADLINES`
3. `VERIFY_RECORDED_AGENT_STATUS`
4. `TRIAGE_UNASSIGNED_TICKETS`

The provider remains disabled by default. Future real execution requires separate human approval,
explicit process opt-in, exact assessment-specific TTY confirmation, and process-bound consent
expiring after 60 seconds before execution. Discovery consent cannot authorize assessment.
Real transport is blocked under pytest, CI, and noninteractive execution. This study changes no
runtime, test, fixture, prompt, model setting, configuration, scorer, or authorization boundary.

## 4. Complete five-run observations

`Absent` means the historical report lacks the field; `null` is an explicitly recorded empty value.
Durations are top-level total workflow measurements, rounded to six decimal places.

| Observation | H1 | H2 | R1 | R2 | R3 |
| --- | --- | --- | --- | --- | --- |
| Required gates | FAIL | PASS | FAIL | PASS | FAIL |
| Classification | Final-answer validation | Completed assessment | Provider/adapter | Completed assessment | Final-answer validation |
| Scenario status | `error` | `answered` | `error` | `answered` | `error` |
| Error code | `invalid_answer` | `null` | `model_unavailable` | `null` | `invalid_answer` |
| Rejection reason | Absent; unknown | `null` | `null` | `null` | `invalid_answer_selection` |
| Runner stop reason | Historical `global_request_budget_exhausted` | `null` | `scenario_gate_failed` | `null` | `scenario_gate_failed` |
| Provider requests / model turns | 2 / 2 | 2 / 2 | 1 / 1 | 2 / 2 | 2 / 2 |
| Tool requests / authorized dispatches | 1 / 1 | 1 / 1 | 0 / 0 | 1 / 1 | 1 / 1 |
| Tool rejections | 0 | 0 | 0 | 0 | 0 |
| Tool selection / exact arguments | true / true | true / true | `null` / `null` | true / true | true / true |
| Recommendation membership/order check | true* | true | false; no source | true | true* |
| Returned-response grounding/provenance/scope check | true* | true | true* | true | true* |
| Input tokens | 1,892 | 1,892 | Unknown | 1,892 | 1,892 |
| Output tokens | 189 | 274 | Unknown | 275 | 322 |
| Total tokens | 2,081 | 2,166 | Unknown | 2,167 | 2,214 |
| Total workflow seconds | 6.230337 | 5.852509 | 3.059326 | 6.990585 | 6.907481 |
| Request budget ceiling reached | Absent | true | false | true | true |
| Provider request prevented | Absent | false | false | false | false |
| Operational writes / attempts | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| Unauthorized tool executions | 0 | 0 | 0 | 0 | 0 |
| Security audit complete | true | true | true | true | true |
| Observed security violations | 0 | 0 | 0 | 0 | 0 |

\* For error responses, positive checks concern retained tool evidence and/or the sanitized returned
error, not the discarded candidate. R1 has no recommendation source; its null tool checks are
unmeasured, and its valid error response does not establish a grounded assessment. See section 6.
Actual billed cost is unverified for every attempt.

## 5. Repeatability-series results

| Population | Attempts | PASS | Final-answer validation failure | Provider/adapter failure |
| --- | --- | --- | --- | --- |
| All five observed assessments | 5 | 2/5 (40%) | 2/5 (40%) | 1/5 (20%) |
| Repeatability series R1–R3 | 3 | 1/3 | 1/3 | 1/3 |
| Reached final-answer validation: H1, H2, R2, R3 | 4 | 2/4 | 2/4 | 0/4 |
| Repeatability attempts reaching final-answer validation: R2, R3 | 2 | 1/2 | 1/2 | 0/2 |
| Provider/adapter failure: R1 only | 1 | 0/1 | 0/1 | 1/1 |

These fractions describe the recorded attempts. R1 remains in the overall and repeatability
denominators. Conditioning on reaching final validation answers a different question and does
not raise overall completion to 2/4. All four dispatched recommendation calls passed tool and
exact-argument checks; only two produced accepted assessments. The repeatability sequence was
provider/adapter failure, PASS, then final-answer failure, so repeated completion was not demonstrated.

These are a small, manually initiated observational series on one scenario. They are not established
independent random samples, a production quality estimate, or a basis for statistical confidence.
The diagnostic improvement between H1 and H2 does not establish a causal change in model quality.

## 6. Final-answer validation failures

### Confirmed boundaries

H1 reached one correct recommendation dispatch and ended with `invalid_answer`. Its report lacks
a rejection reason; its exact cause remains unknown. Neither R3's label nor an offline reference-order
reproduction can retrospectively classify H1.

R3 reached the correct tool with exact arguments, and its recommendation membership/order check
confirms the tool returned all four canonical codes. Its final draft was rejected with public
`invalid_answer` and internal `invalid_answer_selection`. In
[`agent_runtime._render`](../src/ai_operations_agent/agent_runtime.py), the following are **all**
guards that emit that internal reason, in execution order:

| Guard | Concrete condition | Meaning during `queue_assessment` |
| --- | --- | --- |
| Inability draft | `kind == "inability"` and any of: workflow is not `unsupported`, inability is not `unsupported_capability`, or references/codes/blocks are nonempty | Every inability draft is rejected because assessment is not the unsupported workflow |
| Common answer selection | For other kinds, inability is non-null **or** references contain duplicate equal `EvidenceReference` values | Either condition rejects before recommendation selection or reference layout is checked; reference equality includes source, kind, and pointer |
| Discovery state | `kind == "discovery"` and workflow is not `discovery`, discovery evidence is missing, or assessment evidence already exists | Every discovery draft is rejected because the requested workflow is assessment |
| Discovery selection | After the discovery-state guard passes, references differ from the complete discovery references or recommendation codes are nonempty | This guard is for valid discovery state; it cannot be reached under `queue_assessment` because the previous guard rejects first |
| Queue-focus state | The remaining schema-valid kind is `queue_focus`; workflow is not `queue_assessment`, assessment evidence is missing, or selected target is missing | Missing assessment evidence or target can reject an answer offered before valid assessment state; after successful dispatch and capture, evidence and target are present |

The schema permits only `inability`, `discovery`, and `queue_focus`. After R3's completed assessment
dispatch, the candidate-level possibilities consistent with this boundary include wrong kind,
a non-null inability on another kind, or duplicate references. The label does not identify which
condition occurred or whether later checks would also have failed. No specific rejected candidate
contents are retained or inferred here.

Missing, extra, or reordered recommendation codes instead map to
`recommendation_selection_mismatch`, when reached. An invalid mandatory assessment/recommendation
reference prefix or invalid optional metric suffix maps to `assessment_reference_layout_mismatch`.
Wrong explanation blocks map to `explanation_blocks_mismatch`. Rendered-response/private-ledger
failures and unclassified rendering exceptions have their own labels. Malformed provider JSON or
schema-invalid drafts can fail earlier in the adapter as `model_unavailable` and never reach `_render`.
Thus R3's observed label does not establish incorrect recommendation codes or reference ordering.

### Why positive error-response checks do not certify the candidate

[`evaluation.score_scenario`](../src/ai_operations_agent/evaluation.py) compares the retained
recommendation source with the expected catalog. It checks final recommendation evidence
additionally only when status is `answered`. Grounding/provenance/scope validates the returned
response and retained sources; an error can pass while containing no operational evidence or
claims. H1 and R3 pass those checks for the returned error, while terminal-status,
error-classification, and no-unexpected-error checks fail against the expected successful answer.
The scorer never certifies the discarded draft. R2 and H2 pass these gates as accepted answers.

### Offline reproduction and control

Existing official SDK MockTransport tests use the original synthetic scenario and unchanged
runtime/scorer. The following candidate descriptions concern mocked selections only:

| Existing diagnostic case | Observed offline result |
| --- | --- |
| Discovery kind after the correct assessment dispatch | `invalid_answer` / `invalid_answer_selection` |
| Non-null inability on a queue-focus answer after that dispatch | `invalid_answer` / `invalid_answer_selection` |
| Missing or reversed recommendation codes | `recommendation_selection_mismatch` |
| Reversed references or unsupported reference | `assessment_reference_layout_mismatch` |
| Incorrect explanation block selection/order | `explanation_blocks_mismatch` |
| Copy supplied assessment/metrics/recommendations reference order unchanged | `assessment_reference_layout_mismatch` |
| Required assessment/recommendation prefix followed by valid optional metrics, also with reversed optional metrics | PASS |
| Unchanged valid assessment control at exactly two requests | PASS; no prevented continuation |

The rejection assertions verify two provider requests/model turns, one tool request/dispatch,
no tool rejection, complete security audit, zero observed security violations, ceiling reached,
and no prevented request or repair. The independent assessment control additionally verifies
all applicable checks, the original scenario/scorer, and zero operational writes/attempts.
The existing reference-layout test explains a separate boundary; it is not evidence of R3's cause.
No test reproduces the exact historical live draft. Duplicate-reference and inability-kind guards
above are source-traced here; the existing representative selection tests exercise wrong kind and
non-null inability. No tests were added or changed to expand coverage.

## 7. Provider/adapter failure and evidence limits

R1 records one provider request and one model turn, no tool requests or dispatches, unknown input,
output, and total usage, and `model_unavailable` with null rejection reason. The stop reason is
`scenario_gate_failed`; the request ceiling was not reached and no provider request was prevented.
It did not reach runtime final-answer validation and is not a final-answer rejection.

[`OpenAIResponsesAdapter.next_turn`](../src/ai_operations_agent/openai_responses.py) groups
non-timeout SDK authorization, quota, service, transport, configuration, and response-protocol
exceptions into `ModelFailure(reason="unavailable")`. The runtime maps that failure to
`model_unavailable`; adapter-scope/turn exceptions also have that public mapping. Recognized
timeouts map separately to `deadline_exceeded`. Decode failures include incomplete responses,
unsupported/ambiguous output, malformed tool arguments, and malformed structured answers.
Consequently the same public code can represent a provider failure or a local adapter rejection.

Generic early unavailable paths also include disabled/closed adapters, exhausted request budgets,
and an absent/blank process credential before client construction. However, R1's recorded request
count is incremented only after client preparation and budget reservation, immediately before
the SDK call. Its flags do not show budget denial. This narrows the observed stage; it does not
prove a request reached the service. An incorrect credential could fail later, but a completely
absent/blank credential at that early check would ordinarily return before counting any request.

The operator suspected an incorrect or missing API credential. That remains **unconfirmed**.
The allowed telemetry contains no discriminating provider status or raw exception. It establishes
neither HTTP 401, confirmed authentication failure, quota exhaustion, provider outage, nor a
specific protocol defect. Null usage is not proof of zero consumed or billed tokens. No credentials,
raw errors, provider responses, or billing data were inspected to pursue the hypothesis.

Existing SDK MockTransport cases independently verify the shared unavailable mapping for several
HTTP error statuses, connection/protocol failures, incomplete responses, and invalid structured
content; timeout controls verify the separate deadline mapping. These illustrate diagnostic
ambiguity, not the historical cause. No expanded logging or sensitive telemetry is introduced.

## 8. Sanitized diagnostics: coverage and limits

The closed taxonomy in [`answer_diagnostics.py`](../src/ai_operations_agent/answer_diagnostics.py)
accepts only trusted internal enum values. Request-local
[`RuntimeTrace`](../src/ai_operations_agent/agent_telemetry.py) retains fixed event labels,
fingerprints, counts, usage, and a rejection label; reports omit raw candidates and exception text.
This audit used aggregate/check fields, without inspecting event arrays or fingerprint contents.

| Diagnostic | What it identified | What it cannot establish |
| --- | --- | --- |
| H1 `invalid_answer`, no closed label | Final-answer validation failed | Specific guard, rejected selections, or root cause |
| R3 `invalid_answer_selection` | First encountered selection guard in runtime rendering | Which grouped condition, full candidate, or all other defects |
| R1 `model_unavailable`, null rejection | Adapter/model boundary failure before any tool dispatch | Credential, transport, quota, service, or protocol root cause |
| Ceiling reached / request prevented | Budget consumption separately from an actual denied continuation | Provider-side cancellation or a billing cap |
| Complete security audit / zero violations | No violations detected by implemented checks in these invocations | Universal safety or accepted final-answer correctness |

The first historical report predates the newer rejection and budget fields. Its old
`global_request_budget_exhausted` stop reason can reflect reaching the ceiling on a failed scenario;
it does not establish an attempted third request. H1 remains recorded as a final-answer failure
with missing diagnostics. Better failure reporting does not establish remediation or improved model
quality. The study preserves the original [diagnostic comparison](LIVE_ASSESSMENT_COMPARATIVE_CASE_STUDY.md).

## 9. Token usage, duration, and request accounting

Across all five attempts, reports record **9 provider requests/model turns and 4 authorized tool
dispatches**. R1–R3 account for 5 requests/turns and 2 dispatches. The four attempts reaching final
validation account for 8 requests/turns and all 4 dispatches. Failed SDK requests count toward the
local request ceiling; a model-turn count can include a failure outcome rather than a usable answer.

Known usage for H1, H2, R2, and R3 sums to 7,568 input + 1,060 output = **8,628 tokens**.
For R2 and R3 alone it is 3,784 input + 597 output = **4,381 tokens**. These are known subtotals;
the five-attempt and three-attempt aggregates remain unknown because R1 usage is unknown.
[`total_usage`](../src/ai_operations_agent/agent_telemetry.py) deliberately preserves an unknown
aggregate field if any contributing request lacks it. Returned usage is not invoice evidence;
there is no verified actual cost or measured billing cap.

All-attempt workflow durations span 3.059326–6.990585 seconds; the four attempts reaching validation
span 5.852509–6.990585 seconds. The shorter unavailable attempt did less workflow work.
These top-level runner durations include setup, execution, scoring, and cleanup, and differ from
nested scenario duration and per-request latency. The sample provides no latency distribution
or causal performance comparison. Cooperative deadlines do not prove provider-side cancellation.

H2, R2, and R3 reached the 2/2 request ceiling with `provider_request_prevented=false`.
Both PASS and FAIL are compatible with consuming the final permitted request. R1 used 1/2.
H1 used two requests but lacks the newer flags; its historical stop label supplies no missing
prevention measurement. [`ProviderRequestBudget`](../src/ai_operations_agent/provider_budget.py)
separates reservation from a denied continuation. No retry or repair is inferred or authorized.

## 10. Security observations and limitations

All five reports record complete security audits, zero observed security violations, zero
unauthorized tool executions, and zero operational writes/attempts. Four attempts dispatched the
authorized read-only recommendation tool; R1 dispatched nothing. Tool selection/argument checks
are unmeasured in R1, so five correct tool selections are not claimed.

The scorer observes read-only execution, source provenance/scope, trace completeness, and business
state before/after the runtime. Fixture setup/cleanup writes are outside that operational audit.
A rejected candidate can coexist with a clean security audit, and a clean error response is not
a completed assessment. These observations are bounded by the implemented audit and retained
metadata; they are not penetration testing, universal safety, or proof that every possible violation
would be detected. No real customer data, public deployment, or operational action was evaluated.

## 11. Threats to validity

- Five manual attempts on one synthetic scenario provide narrow coverage. The series is not an
  established independent random sample; no production rate or confidence interval is justified.
- H1 predates the diagnostic change. Shared model/dataset identifiers do not guarantee equal
  service conditions, immutable weights, credential state, or complete environment equivalence.
- Git metadata reports clean tracked revisions, not a full environment or dependency lock. No
  intentional R1–R3 changes were reported; unobserved conditions cannot be reconstructed.
- Raw rejected drafts and discriminating provider errors were not retained in the permitted evidence.
  Grouped labels and positive error-response checks cannot recover them.
- Excluding R1 would change the question and overstate completion. Its unknown usage must not
  become zero in token or cost comparisons; failure duration is not successful-answer latency.
- Mocked transport tests validate fixed contracts, not live probability, exact historical outputs,
  production PostgreSQL behavior, billing, or additional live outcomes. No PostgreSQL integration
  test was run during the local documentation audit.

## 12. Engineering findings and next research questions

- Assess completion, final-answer validation, provider/adapter availability, and security separately.
  Correct dispatch and deterministic recommendations are necessary but insufficient for PASS.
- Preserve conditional denominators and missing evidence. H1 remains unclassified; R1 remains
  in the series; R3 identifies a boundary without revealing the candidate.
- Keep rejection rules strict. Existing mocked wrong-kind and inability selections fail after a
  correct tool dispatch, while valid controls pass under the same scenario and limits.
- Diagnostic coverage improved the ability to locate a rejection boundary. It did not establish
  improved model quality, a fix for H1, or production reliability.
- Future research questions include which selection guards recur, what evaluation design would
  cover more synthetic cases, and whether a separately reviewed privacy-preserving distinction
  could reduce adapter ambiguity. No instrumentation, prompt change, relaxed scorer, retry,
  fallback, repair, or further experiment is implemented here. Any live evaluation needs separate
  human approval and a separately reviewed scope.

## 13. Evidence audit, offline reproduction, and references

Only the five named historical assessment reports were audited. The following source handles
identify ignored evidence; no generated report is linked, staged, copied into tracked files, or
published. File bytes were hashed without displaying contents, and sizes and modification times
were recorded **before** field extraction. UTC modification times are filesystem metadata, not
independently verified execution timestamps.

| Run | Evidence handle | Bytes | Modification time (UTC) | SHA-256 |
| --- | --- | --- | --- | --- |
| H1 | `live-assessment-first` | 6,468 | 2026-11-08T01:34:22.252076991Z | `f11605dc647cad025e0ba307140d68c8f968b59ec971a279c02a0358a8cc5c0e` |
| H2 | `live-assessment-second` | 6,698 | 2026-11-08T03:54:36.312847172Z | `11b8c252563d2340232f5419bc3408860a677103523b8e23bc11c1f3765ee63b` |
| R1 | `repeatability-run-01` | 5,883 | 2026-11-15T02:42:36.419650006Z | `43932277be73f40f86ae28bc68b5fc1f221c6521777fb8285582ac0115b40e9e` |
| R2 | `repeatability-run-02` | 6,698 | 2026-11-15T02:48:06.228716296Z | `4556d9897403887e1ac52c7b6327d7ce82f7a52be6dee2be2361976fc2dc9003` |
| R3 | `repeatability-run-03` | 6,751 | 2026-11-15T02:50:37.451318070Z | `c7896a0b17a045183a8ccfb903bfdadafcd0b8239d8e0288132fb9111b981752` |

The field allowlist covered fixed model/dataset/fixture/scenario/workflow identifiers;
outcome/status/error and closed rejection reason; request/turn/tool counts; fixed check booleans;
security audit completeness and violation counts; operational writes/attempts and unauthorized
execution counts; token usage and elapsed duration; budget flags; and reported Git commit/dirty
state. No raw events, source snapshots, provider transcripts/reasoning, arbitrary text, exception
contents, credential data, or sensitive operational identifiers were inspected or copied.

All five files were present. There were **no discrepancies** with supplied values after rounding
duration to six decimals. Additional allowlisted fields corroborated R1 status/zero writes and
R2/R3 two model turns; H1's missing rejection/budget fields remain explicitly absent.

| Run | Reported Git commit | Reported dirty state |
| --- | --- | --- |
| H1 | `841b70b7731274332701984b88ec11d1061e0b73` | false |
| H2 | `0981f24fc598ae2b290701410d71f757e2b22b6a` | false |
| R1, R2, R3 | `a4359b65d08145ee8a3f1f731f942ce305c14244` | false |

For an offline reproduction from the repository root, using the existing development environment
with its optional SDK already installed:

```bash
repo="$PWD"
scratch="$(mktemp -d)"
(
  cd "$scratch" || exit 1
  /usr/bin/env -i PATH=/usr/bin:/bin \
    DATABASE_URL='sqlite+pysqlite:///:memory:' \
    PYTHONPATH="$repo/src:$repo/tests" \
    "$repo/.venv/bin/python" -m pytest -q -p no:cacheprovider \
    "$repo/tests/test_failure_diagnostics.py" \
    "$repo/tests/test_assessment_live_evaluation.py::test_success_reuses_runtime_original_expectation_and_independent_scorer" \
    "$repo/tests/test_openai_responses.py::test_sdk_errors_sanitized_without_retries" \
    "$repo/tests/test_openai_responses.py::test_sdk_timeout_and_network_failure" \
    "$repo/tests/test_openai_responses.py::test_incomplete_failed_truncated_responses" \
    "$repo/tests/test_openai_responses.py::test_invalid_structured_content"
)
```

This exact selection passed **68 existing tests**, with no skips, in a temporary working directory
and cleared process environment. Existing fixtures block real provider networking, use verified
official SDK MockTransport injection for smoke execution, and supply only mocked credentials.
All databases used were private in-memory SQLite. One existing Starlette/httpx deprecation warning
was emitted; no dependencies were changed. No live API call, live credential access, historical
report regeneration, existing database operation, or billing action occurred.

Validation also checked the complete documentation diff/whitespace, internal Markdown targets and
anchors, historical consistency, numerical classifications, and new public text for secrets/private
paths. After validation, SHA-256, byte size, nanosecond modification time, and file mode matched
the pre-read records for every historical report. Reports remain ignored and untracked. Only this
study, README status/links, and a small additive architecture note changed. No commit or publication
occurred during that local implementation audit.

Related tracked evidence for independent review:

- [Original discovery case study](LIVE_DISCOVERY_CASE_STUDY.md) and
  [historical assessment comparison](LIVE_ASSESSMENT_COMPARATIVE_CASE_STUDY.md), unchanged.
- [Assessment preparation runbook](CONTROLLED_ASSESSMENT_RUNBOOK.md), unchanged; its no-live-assessment
  statement describes that earlier preparation milestone. The PR #11-scoped statement in
  [guardrails](../AGENTS.md) likewise describes the discovery experiment.
- [Architecture and scope](ARCHITECTURE.md), [current status](../README.md),
  [versioned scenario](../src/ai_operations_agent/evaluation_data/v1.json),
  [independent dataset contracts](../src/ai_operations_agent/evaluation_dataset.py), and
  [private synthetic fixtures](../src/ai_operations_agent/evaluation_fixtures.py).
- [Runtime guards](../src/ai_operations_agent/agent_runtime.py),
  [draft schema](../src/ai_operations_agent/agent_schemas.py),
  [closed rejection taxonomy](../src/ai_operations_agent/answer_diagnostics.py),
  [sanitized telemetry](../src/ai_operations_agent/agent_telemetry.py),
  [adapter failure mapping](../src/ai_operations_agent/openai_responses.py),
  [scorer](../src/ai_operations_agent/evaluation.py),
  [runner/report accounting](../src/ai_operations_agent/live_evaluation.py), and
  [request budget](../src/ai_operations_agent/provider_budget.py).
- [Diagnostic reproductions](../tests/test_failure_diagnostics.py),
  [valid assessment control](../tests/test_assessment_live_evaluation.py), and
  [SDK failure/protocol controls](../tests/test_openai_responses.py).
