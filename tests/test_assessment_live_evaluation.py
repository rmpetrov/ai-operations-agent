"""Assessment preparation with the official SDK and verified MockTransport only."""

import builtins
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from time import monotonic
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session
from test_live_evaluation import consent, script
from test_live_evaluation import (
    no_default_database_or_mutable_runtime_session as no_default_database_or_mutable_runtime_session,
)
from test_offline_agent_evaluations import focus
from test_openai_responses import MockProvider, call, message, response
from test_openai_responses import no_provider_network as no_provider_network

from ai_operations_agent import evaluation, evaluation_fixtures, live_evaluation, tools
from ai_operations_agent.agent_schemas import EvidenceReference, StructuredAnswerDraft
from ai_operations_agent.agent_telemetry import RuntimeTrace
from ai_operations_agent.evaluation_dataset import CANONICAL_CODES, load_dataset
from ai_operations_agent.evaluation_fixtures import business_snapshot
from ai_operations_agent.live_evaluation import (
    ASSESSMENT_CONFIRMATION,
    ASSESSMENT_CONSENT_TTL_SECONDS,
    ASSESSMENT_OPT_IN_VALUE,
    ASSESSMENT_OPT_IN_VARIABLE,
    CONFIRMATION,
    MODEL_ID,
    OPT_IN_VALUE,
    OPT_IN_VARIABLE,
    AssessmentConsent,
    SmokeSettings,
    execution_plan,
    report_markdown,
    run_smoke,
)
from ai_operations_agent.models import Queue
from ai_operations_agent.openai_responses import OpenAIResponsesAdapter, ResponsesSettings
from ai_operations_agent.provider_budget import ProviderRequestBudget


def settings(**overrides):
    return SmokeSettings(**{"provider_enabled": True, "execution_mode": "assessment-only", **overrides})


def permission(**overrides):
    return AssessmentConsent(**{"opted_in": True, "confirmation": ASSESSMENT_CONFIRMATION, **overrides})


def assessment_script(queue_ids):
    outcomes = script()[2:4]
    outcomes[0]["output"][-1] = call(
        "get_queue_recommendations", json.dumps({"queue_id": queue_ids["all"]}), "selected",
    )
    return outcomes


def assessment(outcomes=assessment_script, *, config=None, **kwargs):
    provider = MockProvider([])
    original = evaluation.evaluate_scenario

    def execute(scenario, fixtures, **options):
        provider.fixture_ids = fixtures.queue_ids.copy()
        # Supply extra outcomes to prove failures never consume recovery/repair turns.
        provider.script = iter(outcomes(fixtures.queue_ids) + assessment_script(fixtures.queue_ids))
        return original(scenario, fixtures, **options)

    with patch.object(evaluation, "evaluate_scenario", side_effect=execute) as execute_spy:
        report = run_smoke(config or settings(), permission(),
                           client_factory=provider.factory, **kwargs)
    assert execute_spy.call_count == 1
    assert all(client.is_closed() for client in provider.clients)
    assert report["execution_mode"] == "assessment-only" and report["scenario_count"] == 1
    assert [s["scenario_id"] for s in report["planned_scenarios"]] == ["selected-all-signals"]
    return report, provider


def test_success_reuses_runtime_original_expectation_and_independent_scorer():
    original = next(s for s in load_dataset().scenarios if s.id == "selected-all-signals")
    _, scenarios = live_evaluation.smoke_scenarios(settings())
    assert scenarios == (original,) and original.expected.recommendation_codes == CANONICAL_CODES
    with (patch.object(evaluation, "score_scenario", wraps=evaluation.score_scenario) as score,
          patch.object(evaluation, "run_agent", wraps=evaluation.run_agent) as runtime):
        report, provider = assessment()
    assert score.call_count == runtime.call_count == 1
    assert score.call_args.args[0] == original
    access = runtime.call_args.args[1]
    assert access.selected_queue_id == provider.fixture_ids[original.selected_queue]
    assert access.allowed_queue_ids == frozenset(provider.fixture_ids[k]
                                               for k in original.authorized_queue_scope)
    limits = runtime.call_args.kwargs["limits"]
    assert limits.max_model_turns == 2 and limits.max_tool_requests == 1
    assert report["required_gates_passed"] and report["security_audit_complete"]
    assert report["terminal_outcome"] == "passed" and report["stop_reason"] is None
    assert report["passed"] == report["executed_scenario_count"] == 1 and report["failed"] == 0
    assert report["not_run"] == report["unscored_attempts"] == []
    assert report["provider_request_count"] == report["model_turn_count"] == len(provider.requests) == 2
    assert report["tool_request_count"] == report["tool_dispatch_count"] == 1
    assert report["tool_rejection_count"] == 0
    result = report["results"][0]
    assert all(value is True for value in result["checks"].values() if value is not None)
    assert result["security_violations"] == []
    assert result["operational_writes"] == result["operational_write_attempts"] == 0
    assert report["usage"] == {"input_tokens": 22, "output_tokens": 14, "total_tokens": 36}
    assert report["cost"]["estimated_usd"] is None
    first, final = [body for body, _ in provider.requests]
    assert [t["name"] for t in first["tools"]] == ["get_queue_recommendations"]
    assert final["tools"] == [] and final["tool_choice"] == "none"
    context = json.loads(first["input"][0]["content"])
    assert context["trusted_selection"] == {"queue_id": access.selected_queue_id}
    assert context["workflow"] == "queue_assessment" and context["queue_name"] is None
    for body, extensions in provider.requests:
        assert body["model"] == MODEL_ID and body["max_output_tokens"] == 2048
        assert body["store"] is body["parallel_tool_calls"] is False
        assert extensions["timeout"]["read"] <= 10
    assert all(c["max_retries"] == 0 and c["base_url"] == "https://api.openai.com/v1"
               for c in provider.client_options)


def test_selection_resolves_fixture_identity_even_when_ids_shift(monkeypatch):
    original = evaluation_fixtures.owned_fixtures

    @contextmanager
    def shifted(engine, **kwargs):
        with Session(engine) as seed:
            queue = Queue(name="Synthetic unrelated baseline", sla_minutes=30)
            seed.add(queue)
            seed.commit()
        with original(engine, **kwargs) as fixtures:
            yield fixtures

    monkeypatch.setattr(evaluation_fixtures, "owned_fixtures", shifted)
    report, provider = assessment()
    assert report["required_gates_passed"]
    assert json.loads(provider.requests[0][0]["input"][0]["content"])["trusted_selection"] == {
        "queue_id": provider.fixture_ids["all"],
    }


@pytest.mark.parametrize("attack", [
    "missing_recommendation", "reordered_recommendations", "duplicate_recommendation",
    "fabricated_source", "fabricated_pointer", "missing_assessment_reference",
    "missing_recommendation_reference", "reordered_references", "wrong_reference_kind",
    "premature_answer", "malformed_answer", "extra_answer_field", "duplicate_answer_field",
    "wrong_tool", "discovery_after_assessment", "repeated_assessment", "multiple_calls",
    "call_and_answer", "hosted_tool", "malformed_arguments", "string_queue_id",
    "extra_argument", "duplicate_argument", "truncation", "http_first", "http_second", "timeout",
])
def test_invalid_assessment_is_terminal_without_repair_or_discovery(attack):
    requests, dispatches = 2, 1

    def outcomes(ids):
        nonlocal requests, dispatches
        values = assessment_script(ids)
        draft = focus(source="eval_1")
        if attack in {"missing_recommendation", "reordered_recommendations", "duplicate_recommendation"}:
            codes = draft.recommendation_codes
            codes = (codes[:-1] if attack == "missing_recommendation" else
                     codes[::-1] if attack == "reordered_recommendations" else codes + codes[:1])
            draft = draft.model_copy(update={"recommendation_codes": codes})
        elif attack.startswith("fabricated"):
            ref = EvidenceReference(source_id="invented" if attack == "fabricated_source" else "eval_1",
                                    kind="assessment", pointer="/999" if attack == "fabricated_pointer"
                                    else "/assessment")
            draft = draft.model_copy(update={"references": (ref,) + draft.references[1:]})
        elif attack in {"missing_assessment_reference", "missing_recommendation_reference",
                        "reordered_references", "wrong_reference_kind"}:
            refs = draft.references
            refs = (refs[1:] if attack == "missing_assessment_reference" else
                    refs[:-1] if attack == "missing_recommendation_reference" else
                    refs[::-1] if attack == "reordered_references" else
                    (EvidenceReference(source_id="eval_1", kind="queue", pointer="/0"),) + refs[1:])
            draft = draft.model_copy(update={"references": refs})
        elif attack == "premature_answer":
            values[0] = response(message(draft))
            requests, dispatches = 1, 0
            return values
        elif attack in {"malformed_answer", "extra_answer_field", "duplicate_answer_field"}:
            raw = ('{"type":"answer"}' if attack == "malformed_answer" else
                   draft.model_dump_json()[:-1] + ',"inability":null}'
                   if attack == "duplicate_answer_field" else
                   draft.model_dump_json()[:-1] + ',"untrusted":"SECRET"}')
            values[1] = response(message(text=raw))
            return values
        elif attack in {"wrong_tool", "discovery_after_assessment", "repeated_assessment"}:
            index = 0 if attack == "wrong_tool" else 1
            values[index] = response(call(call_id="discover")) if attack != "repeated_assessment" else (
                response(call("get_queue_recommendations", json.dumps({"queue_id": ids["all"]}), "again")))
            requests, dispatches = index + 1, index
            return values
        elif attack in {"multiple_calls", "call_and_answer", "hosted_tool"}:
            values[0] = (response(values[0]["output"][-1], call(call_id="extra"))
                         if attack == "multiple_calls" else
                         response(values[0]["output"][-1], message(draft))
                         if attack == "call_and_answer" else
                         response({"type": "web_search_call", "id": "hosted", "status": "completed"}))
            requests, dispatches = 1, 0
            return values
        elif attack in {"malformed_arguments", "string_queue_id", "extra_argument", "duplicate_argument"}:
            raw = {"malformed_arguments": '{"queue_id":',
                   "string_queue_id": json.dumps({"queue_id": str(ids["all"])}),
                   "extra_argument": json.dumps({"queue_id": ids["all"], "model": "SECRET"}),
                   "duplicate_argument": f'{{"queue_id":{ids["all"]},"queue_id":{ids["all"]}}}'}[attack]
            values[0] = response(call("get_queue_recommendations", raw))
            requests, dispatches = 1, 0
            return values
        elif attack == "truncation":
            values[1] = response(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
            return values
        elif attack.startswith("http") or attack == "timeout":
            index = 1 if attack == "http_second" else 0
            values[index] = (httpx.ReadTimeout("SECRET") if attack == "timeout" else
                             (503, {"error": {"message": "SECRET"}}))
            requests, dispatches = index + 1, index
            return values
        values[1] = response(message(draft))
        return values

    report, provider = assessment(outcomes)
    assert not report["required_gates_passed"] and report["terminal_outcome"] == "failed"
    assert report["provider_request_count"] == len(provider.requests) == requests
    assert report["tool_dispatch_count"] == dispatches
    assert report["executed_scenario_count"] == report["failed"] == 1 and report["passed"] == 0
    assert report["unscored_attempts"] == report["not_run"] == []
    assert report["security_audit_complete"]
    assert report["results"][0]["status"] == "error"
    assert all(e.get("tool") in {None, "get_queue_recommendations"}
               for e in report["results"][0]["events"])
    assert "SECRET" not in json.dumps(report) + report_markdown(report)


@pytest.mark.parametrize("queue_key", ["healthy", "hidden", "missing"])
def test_wrong_or_unauthorized_queue_is_rejected_before_dispatch(queue_key):
    def outcomes(ids):
        qid = max(ids.values()) + 1 if queue_key == "missing" else ids[queue_key]
        return [response(call("get_queue_recommendations", json.dumps({"queue_id": qid})))]

    report, provider = assessment(outcomes)
    result = report["results"][0]
    assert not result["passed"] and result["error_code"] == "queue_unavailable"
    assert result["checks"]["exact_arguments"] is False
    assert report["tool_dispatch_count"] == 0 and report["tool_rejection_count"] == 1
    assert result["unauthorized_tool_executions"] == 0 and len(provider.requests) == 1


def test_lower_budget_exhaustion_stops_before_second_request():
    report, provider = assessment(config=settings(max_provider_requests=1))
    assert report["provider_request_count"] == len(provider.requests) == 1
    assert report["stop_reason"] == "global_request_budget_exhausted"
    assert report["failed"] == 1 and not report["required_gates_passed"]


def test_failed_sdk_requests_consume_shared_two_request_budget_across_scopes():
    budget = ProviderRequestBudget(2)
    provider = MockProvider([(429, {"error": {"message": "SECRET"}})] * 3)
    for _ in range(3):
        adapter = OpenAIResponsesAdapter(
            ResponsesSettings(enabled=True, model_id=MODEL_ID, max_requests=2),
            client_factory=provider.factory, request_budget=budget,
        )
        with adapter.open_run() as scoped:
            turn = scoped.next_turn(({"role": "user", "request": {
                "question": "Synthetic assessment", "workflow": "queue_assessment", "queue_name": None,
            }},), ("get_queue_recommendations",), StructuredAnswerDraft.model_json_schema(), monotonic() + 30)
            assert turn.type == "model_failure"
    assert budget.used == len(provider.requests) == len(provider.clients) == 2
    assert all(client.is_closed() for client in provider.clients)


@pytest.mark.parametrize("mutation", [
    "missing", "discovery", "wrong_confirmation", "wrong_mode", "expired", "future", "other_process",
    "disabled", "unvalidated_budget", "unvalidated_model",
])
def test_rejected_preconditions_read_no_credentials_and_create_no_client_or_database(monkeypatch, mutation):
    config, granted = settings(), permission()
    if mutation == "missing":
        granted = None
    elif mutation == "discovery":
        granted = consent()
    elif mutation in {"wrong_confirmation", "wrong_mode", "expired", "future", "other_process"}:
        updates = {"wrong_confirmation": {"confirmation": CONFIRMATION},
                   "wrong_mode": {"execution_mode": "discovery-only"},
                   "expired": {"confirmed_at": monotonic() - ASSESSMENT_CONSENT_TTL_SECONDS - 1},
                   "future": {"confirmed_at": monotonic() + 60},
                   "other_process": {"process_id": os.getpid() + 1}}[mutation]
        granted = granted.model_copy(update=updates)
    elif mutation == "disabled":
        config = settings(provider_enabled=False)
    else:
        config = config.model_copy(update={"max_provider_requests": 9} if mutation == "unvalidated_budget"
                                   else {"model_id": "SECRET"})
    original_get = os.environ.get

    def no_key(name, *args):
        assert name != "OPENAI_API_KEY", "rejected precondition read credentials"
        return original_get(name, *args)

    monkeypatch.setattr(os.environ, "get", no_key)
    monkeypatch.setattr(evaluation_fixtures, "synthetic_database", lambda: pytest.fail("database created"))
    provider = MockProvider([])
    report = run_smoke(config, granted, client_factory=provider.factory)
    assert report["stop_reason"] and not report["required_gates_passed"]
    assert report["provider_request_count"] == report["executed_scenario_count"] == 0
    assert provider.clients == provider.requests == []
    assert not report["security_audit_complete"]
    assert "SECRET" not in json.dumps(report)


@pytest.mark.parametrize("value", [3, 9, 0, True, 2.0])
def test_assessment_budget_cannot_be_raised_or_coerced(value):
    with pytest.raises(ValidationError):
        settings(max_provider_requests=value)


@pytest.mark.parametrize("case", [
    "confirmed", "missing_marker", "discovery_marker", "wrong_marker", "not_tty_in", "not_tty_out",
    "discovery_confirmation", "wrong_confirmation", "eof", "interrupt",
])
def test_exact_interactive_assessment_consent_without_execution(monkeypatch, case):
    monkeypatch.setattr(live_evaluation, "_automation_context", lambda: False)
    monkeypatch.setenv(ASSESSMENT_OPT_IN_VARIABLE, ASSESSMENT_OPT_IN_VALUE)
    monkeypatch.setenv(OPT_IN_VARIABLE, OPT_IN_VALUE)
    if case in {"missing_marker", "discovery_marker"}:
        monkeypatch.delenv(ASSESSMENT_OPT_IN_VARIABLE)
    elif case == "wrong_marker":
        monkeypatch.setenv(ASSESSMENT_OPT_IN_VARIABLE, OPT_IN_VALUE)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: case != "not_tty_in")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: case != "not_tty_out")
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        if case == "eof":
            raise EOFError
        if case == "interrupt":
            raise KeyboardInterrupt
        return (CONFIRMATION if case == "discovery_confirmation" else
                "no" if case == "wrong_confirmation" else ASSESSMENT_CONFIRMATION)

    monkeypatch.setattr(builtins, "input", answer)
    granted, reason = live_evaluation._cli_consent("assessment-only")
    if case == "confirmed":
        assert isinstance(granted, AssessmentConsent) and reason is None
        assert granted.process_id == os.getpid()
        assert 0 <= monotonic() - granted.confirmed_at <= ASSESSMENT_CONSENT_TTL_SECONDS
        for text in ("selected-all-signals", "at most two provider requests", "read-only",
                     "get_queue_recommendations", "no automatic retries", "API charges"):
            assert text in prompts[0]
    else:
        assert granted is None and reason


@pytest.mark.parametrize("block", ["pytest", "CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST",
                                        "missing_marker", "discovery_marker", "not_tty_in", "not_tty_out"])
def test_real_transport_blocks_before_credentials_client_or_database_in_fresh_process(block, tmp_path):
    code = r'''
import builtins, os, sys
from unittest.mock import patch
from ai_operations_agent.live_evaluation import *
block = sys.argv[1]
if block == "pytest":
    import pytest
os.environ[ASSESSMENT_OPT_IN_VARIABLE] = ASSESSMENT_OPT_IN_VALUE
os.environ[OPT_IN_VARIABLE] = OPT_IN_VALUE
if block in {"missing_marker", "discovery_marker"}:
    del os.environ[ASSESSMENT_OPT_IN_VARIABLE]
elif block in {"CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST"}:
    os.environ[block] = "1"
original_get = os.environ.get
original_import = builtins.__import__
def guarded_get(name, *args):
    assert name != "OPENAI_API_KEY", "credential read"
    return original_get(name, *args)
def guarded_import(name, *args, **kwargs):
    assert name not in {"openai", "ai_operations_agent.evaluation", "ai_operations_agent.config",
                        "ai_operations_agent.evaluation_fixtures", "ai_operations_agent.openai_responses"}
    return original_import(name, *args, **kwargs)
with patch.object(os.environ, "get", guarded_get), patch.object(builtins, "__import__", guarded_import), \
     patch.object(sys.stdin, "isatty", lambda: block != "not_tty_in"), \
     patch.object(sys.stdout, "isatty", lambda: block != "not_tty_out"):
    report = run_smoke(SmokeSettings(provider_enabled=True, execution_mode="assessment-only"),
                       AssessmentConsent(opted_in=True, confirmation=ASSESSMENT_CONFIRMATION))
assert report["stop_reason"] == ("interactive_confirmation_required" if block.startswith("not_tty") else
                                  "missing_explicit_consent" if block.endswith("marker") else
                                  "automated_live_execution_blocked")
assert report["provider_request_count"] == report["executed_scenario_count"] == 0
'''
    env = {k: v for k, v in os.environ.items()
           if k not in {"CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST", "OPENAI_API_KEY"}}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, "-c", code, block], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())


def test_cli_assessment_requires_live_and_writes_blocked_sanitized_reports(monkeypatch, tmp_path, capsys):
    def no_prompt(*args):
        pytest.fail("pytest attempted interactive confirmation")

    monkeypatch.setattr(builtins, "input", no_prompt)
    assert live_evaluation.main(["--execution-mode", "assessment-only", "--output-dir", str(tmp_path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert not plan["provider_enabled"] and not list(tmp_path.iterdir())
    assert live_evaluation.main(["--live", "--execution-mode", "assessment-only",
                                 "--output-dir", str(tmp_path)]) == 1
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["stop_reason"] == "automated_live_execution_blocked"
    assert report["execution_mode"] == "assessment-only" and report["provider_request_count"] == 0
    assert report["not_run"][0]["scenario_id"] == "selected-all-signals"
    assert (tmp_path / "summary.md").read_text() == report_markdown(report)


@pytest.mark.parametrize("args", [["--execution-mode", "mocked-suite"], ["--scenario", "named-assessment"],
                                  ["--max-provider-requests", "9"], ["--queue-id", "SECRET"],
                                  ["--question", "SECRET"], ["--database-url", "SECRET"]])
def test_cli_cannot_override_scope_or_ceiling_or_run_mocked_suite(args, capsys):
    with pytest.raises(SystemExit) as exc:
        live_evaluation.main(["--execution-mode", "assessment-only", *args])
    assert exc.value.code == 2 and "SECRET" not in capsys.readouterr().err


@pytest.mark.parametrize("phase", ["success", "invalid_answer", "scoring_failure", "write_attempt"])
def test_private_synthetic_cleanup_read_only_audit_and_sanitized_reports(monkeypatch, phase):
    original = evaluation_fixtures.owned_fixtures
    cleanups, disposals = [], []

    @contextmanager
    def observed(engine, **kwargs):
        assert str(engine.url) == "sqlite+pysqlite:///:memory:"
        event.listen(engine, "engine_disposed", lambda value: disposals.append(True))
        before = business_snapshot(engine)
        try:
            with original(engine, **kwargs) as fixtures:
                yield fixtures
        finally:
            assert business_snapshot(engine) == before
            cleanups.append(True)

    monkeypatch.setattr(evaluation_fixtures, "owned_fixtures", observed)
    if phase == "scoring_failure":
        def failed_score(*args, **kwargs):
            raise RuntimeError("SECRET scorer details")
        monkeypatch.setattr(evaluation, "score_scenario", failed_score)
    elif phase == "write_attempt":
        def attempted_write(*args, engine, **kwargs):
            with engine.begin() as connection:
                connection.execute(sql_text("UPDATE tickets SET subject = 'SECRET'"))
        monkeypatch.setattr(tools, "execute_tool", attempted_write)

    def outcomes(ids):
        values = assessment_script(ids)
        if phase == "invalid_answer":
            values[1] = response(message(text='{"type":"answer"}'))
        return values

    report, _ = assessment(outcomes)
    assert cleanups == disposals == [True]
    assert report["required_gates_passed"] is (phase == "success")
    assert report["security_audit_complete"] is (phase != "scoring_failure")
    if phase == "scoring_failure":
        assert report["terminal_outcome"] == "incomplete_audit"
        assert report["stop_reason"] == "smoke_execution_failed"
        assert report["unscored_attempts"][0]["provider_request_count"] == 2
        assert report["not_run"] == [] and report["results"] == []
    else:
        assert report["results"][0]["operational_writes"] == 0
        assert report["results"][0]["operational_write_attempts"] == (phase == "write_attempt")
    if phase == "write_attempt":
        assert report["stop_reason"] == "security_violation"
        assert report["provider_request_count"] == 1
    assert json.loads(json.dumps(report, allow_nan=False)) == report
    summary = report_markdown(report)
    for text in ("assessment-only", "selected-all-signals", "recommendations in canonical order",
                 "Provider requests:", "Tool requests:", "Elapsed seconds:",
                 "security audit complete:", "not actual billed cost"):
        assert text in summary
    if report["results"]:
        for text in ("correct tool:", "exact arguments:", "recommendation membership / order:",
                     "grounding / provenance / authorized scope:", "operational writes / attempts:"):
            assert text in summary
    for text in ("SECRET", "mock-provider-sentinel", "opaque-mocked-reasoning", "encrypted_content",
                 "Synthetic ticket", "Synthetic operator", "Review the configured synthetic queue."):
        assert text not in json.dumps(report) + summary
    assert report["elapsed_seconds"] >= sum(r["elapsed_seconds"] for r in report["results"])


def test_unknown_usage_stays_unknown_and_cleanup_failure_cannot_pass(monkeypatch):
    original = evaluation_fixtures.synthetic_database

    @contextmanager
    def failed_cleanup():
        with original() as fixtures:
            yield fixtures
        raise RuntimeError("SECRET cleanup")

    monkeypatch.setattr(evaluation_fixtures, "synthetic_database", failed_cleanup)

    def outcomes(ids):
        values = assessment_script(ids)
        del values[1]["usage"]
        return values

    report, _ = assessment(outcomes)
    assert report["passed"] == 1 and not report["required_gates_passed"]
    assert report["terminal_outcome"] == "incomplete_audit" and not report["security_audit_complete"]
    assert all(value is None for value in report["usage"].values())
    assert "SECRET" not in json.dumps(report) + report_markdown(report)


@pytest.mark.parametrize("boundary", ["arguments", "provider_payload", "model_turn"])
def test_assessment_preserves_payload_limits(boundary):
    def outcomes(ids):
        return [response(call("get_queue_recommendations", json.dumps({"padding": "x" * 4096})))
                if boundary == "arguments" else response(message(text="x" * (
                    196608 if boundary == "provider_payload" else 8193)))]

    report, provider = assessment(outcomes)
    assert not report["required_gates_passed"] and len(provider.requests) == 1
    assert report["results"][0]["error_code"] == "model_unavailable"
    assert report["tool_dispatch_count"] == 0


def test_expired_deadline_prevents_client_creation():
    ticks = iter([0.0, 0.0])
    report, provider = assessment(clock=lambda: next(ticks, 31.0))
    assert report["provider_request_count"] == 0 and provider.clients == []
    assert report["results"][0]["error_code"] == "deadline_exceeded"


def test_environment_cannot_change_assessment_scope_target_model_or_endpoint(monkeypatch):
    for name, value in {"DATABASE_URL": "postgresql+psycopg://development.invalid/operations",
                        "OPENAI_BASE_URL": "https://forbidden.invalid", "OPENAI_MODEL": "untrusted",
                        "AGENT_ALLOWED_QUEUE_IDS": "999", "AGENT_SELECTED_QUEUE_ID": "999",
                        "AGENT_MAX_PROVIDER_REQUESTS": "9"}.items():
        monkeypatch.setenv(name, value)
    report, provider = assessment()
    assert report["required_gates_passed"] and report["global_max_provider_requests"] == 2
    assert all(options["base_url"] == "https://api.openai.com/v1" for options in provider.client_options)


@pytest.mark.parametrize("execution_mode", ["discovery-only", "assessment-only"])
def test_dry_runs_block_credentials_dotenv_network_client_and_database_creation(execution_mode, tmp_path):
    code = r'''
import builtins, os, sys
from unittest.mock import patch
original_import = builtins.__import__
original_get = os.environ.get
original_item = type(os.environ).__getitem__
def imports(name, *args, **kwargs):
    assert name not in {"openai", "sqlalchemy", "ai_operations_agent.config", "ai_operations_agent.db",
                        "ai_operations_agent.openai_responses", "ai_operations_agent.evaluation",
                        "ai_operations_agent.evaluation_fixtures"}, "execution import during planning"
    return original_import(name, *args, **kwargs)
def get(name, *args):
    assert name != "OPENAI_API_KEY", "credential read"
    return original_get(name, *args)
def item(self, name):
    assert name != "OPENAI_API_KEY", "credential read"
    return original_item(self, name)
def audit(event, arguments):
    assert event not in {"socket.connect", "socket.getaddrinfo"}, "network"
    if event == "open" and isinstance(arguments[0], (str, bytes)):
        assert os.path.basename(arguments[0]) not in {".env", b".env"}, "dotenv read"
sys.addaudithook(audit)
with patch.object(builtins, "__import__", imports), patch.object(os.environ, "get", get), \
     patch.object(type(os.environ), "__getitem__", item):
    from ai_operations_agent.live_evaluation import main
    assert main([] if sys.argv[1] == "discovery-only" else
                ["--dry-run", "--execution-mode", "assessment-only"]) == 0
'''
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, "-c", code, execution_mode], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["execution_mode"] == execution_mode and plan["provider_requests_executed"] == 0
    assert plan["credential_reads"] == plan["provider_clients_created"] == plan["databases_created"] == 0
    assert plan["global_max_provider_requests"] == plan["max_model_turns_per_scenario"] == 2
    assert plan["max_tool_requests_per_scenario"] == 1 and plan["max_output_tokens_per_request"] == 2048
    assert not plan["provider_enabled"] and not list(tmp_path.iterdir())
    if execution_mode == "assessment-only":
        selected = plan["scenarios"][0]
        assert selected["scenario_id"] == "selected-all-signals" and selected["selected_queue"] == "all"
        assert selected["expected_recommendation_codes"] == list(CANONICAL_CODES)
        assert any(ASSESSMENT_OPT_IN_VARIABLE in gate for gate in plan["required_live_gates"])
        assert plan["assessment_consent_ttl_seconds"] == ASSESSMENT_CONSENT_TTL_SECONDS


def test_assessment_consent_cannot_authorize_discovery(monkeypatch):
    def no_key(*args):
        pytest.fail("mismatched consent accessed credentials")

    monkeypatch.setattr(os.environ, "get", no_key)
    report = run_smoke(SmokeSettings(provider_enabled=True), permission(), client_factory=MockProvider([]).factory)
    assert report["stop_reason"] == "missing_explicit_consent" and report["provider_request_count"] == 0


def test_plan_uses_original_independent_recommendation_expectations():
    plan = execution_plan(settings())
    assert plan["scenarios"][0]["expected_tool_sequences"] == [[{
        "name": "get_queue_recommendations", "queue_key": "all",
    }]]
    assert plan["global_max_output_tokens"] == 4096
    assert plan["live_execution_approved_by_this_milestone"] is False


def test_incomplete_tool_telemetry_fails_security_audit_and_overall_gate(monkeypatch):
    original = RuntimeTrace.event

    def missing_acceptance(self, stage, **kwargs):
        if stage != "tool_accepted":
            return original(self, stage, **kwargs)

    monkeypatch.setattr(RuntimeTrace, "event", missing_acceptance)
    report, _ = assessment()
    assert report["stop_reason"] == "security_violation"
    assert "incomplete_tool_telemetry" in report["results"][0]["security_violations"]
    assert not report["required_gates_passed"] and not report["security_audit_complete"]
    assert report["terminal_outcome"] == "incomplete_audit"
    assert "security audit complete: False" in report_markdown(report)


def test_assessment_mock_seam_rejects_real_transport_before_provider_request():
    openai = pytest.importorskip("openai")
    clients = []

    def factory(**kwargs):
        client = openai.OpenAI(**kwargs, http_client=httpx.Client(trust_env=False))
        clients.append(client)
        return client

    report = run_smoke(settings(), permission(), client_factory=factory)
    assert not report["required_gates_passed"] and report["provider_request_count"] == 0
    assert report["results"][0]["error_code"] == "model_unavailable"
    assert len(clients) == 1 and clients[0].is_closed()


@pytest.mark.parametrize("qid", [True, 0, -1, "7"])
def test_trusted_selection_requires_strict_positive_queue_identity(qid):
    with pytest.raises(ValidationError):
        OpenAIResponsesAdapter(selected_queue_id=qid)
