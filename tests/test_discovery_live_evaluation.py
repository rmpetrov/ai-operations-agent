"""Discovery-only smoke preparation: official SDK, MockTransport, synthetic data only."""

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
from test_live_evaluation import consent, discovery_draft, script
from test_live_evaluation import (
    no_default_database_or_mutable_runtime_session as no_default_database_or_mutable_runtime_session,
)
from test_openai_responses import MockProvider, call, message, response
from test_openai_responses import no_provider_network as no_provider_network

from ai_operations_agent import evaluation, evaluation_fixtures, live_evaluation
from ai_operations_agent.agent_schemas import EvidenceReference, StructuredAnswerDraft
from ai_operations_agent.evaluation_dataset import load_dataset
from ai_operations_agent.live_evaluation import (
    CONFIRMATION,
    MODEL_ID,
    OPT_IN_VALUE,
    OPT_IN_VARIABLE,
    SmokeConsent,
    SmokeSettings,
    execution_plan,
    report_markdown,
    run_smoke,
)
from ai_operations_agent.openai_responses import OpenAIResponsesAdapter, ResponsesSettings
from ai_operations_agent.provider_budget import ProviderRequestBudget


def discovery(provider=None, **overrides):
    provider = provider or MockProvider(script())
    report = run_smoke(SmokeSettings(provider_enabled=True, **overrides), consent(),
                       client_factory=provider.factory)
    assert all(client.is_closed() for client in provider.clients)
    return report, provider


def assert_only_discovery(report):
    assert report["execution_mode"] == "discovery-only"
    assert report["scenario_count"] == 1
    assert report["global_max_provider_requests"] <= 2
    assert [s["scenario_id"] for s in report["planned_scenarios"]] == ["authorized-discovery"]
    serialized = json.dumps(report) + report_markdown(report)
    assert "selected-all-signals" not in serialized and "named-assessment" not in serialized


def test_success_requires_two_requests_one_tool_and_existing_grounded_scorer():
    with (patch.object(evaluation, "evaluate_scenario", wraps=evaluation.evaluate_scenario) as execute,
          patch.object(evaluation, "score_scenario", wraps=evaluation.score_scenario) as score,
          patch.object(evaluation, "run_agent", wraps=evaluation.run_agent) as runtime):
        report, provider = discovery()
    assert_only_discovery(report)
    assert execute.call_count == score.call_count == runtime.call_count == 1
    assert report["required_gates_passed"] and report["security_audit_complete"]
    assert report["terminal_outcome"] == "passed" and report["stop_reason"] is None
    assert report["executed_scenario_count"] == report["passed"] == 1 and report["failed"] == 0
    assert report["not_run"] == report["unscored_attempts"] == []
    assert report["provider_request_count"] == report["model_turn_count"] == len(provider.requests) == 2
    assert report["tool_request_count"] == report["tool_dispatch_count"] == 1
    assert report["tool_rejection_count"] == 0
    assert report["usage"] == {"input_tokens": 22, "output_tokens": 14, "total_tokens": 36}
    assert report["cost"]["estimated_usd"] is None
    assert report["metrics"]["planned_task_completion_rate"] == {"passed": 1, "eligible": 1, "rate": 1.0}
    result = report["results"][0]
    for check in ("terminal_status", "allowed_dispatch_sequence", "discovery_membership_order",
                  "evidence_grounding", "exact_arguments", "trace_final_status"):
        assert result["checks"][check] is True
    assert result["security_violations"] == []
    limits = runtime.call_args.kwargs["limits"]
    assert limits.max_model_turns == 2 and limits.max_tool_requests == 1
    first, final = [body for body, _ in provider.requests]
    assert [t["name"] for t in first["tools"]] == ["list_queues"]
    assert final["tools"] == [] and final["tool_choice"] == "none"
    trusted = next(item for item in final["input"] if item.get("type") == "function_call_output")
    evidence = json.loads(trusted["output"])
    assert evidence["source"]["tool_name"] == "list_queues"
    assert [q["id"] for q in evidence["source"]["snapshot"]] == [2, 7]
    assert evidence["references"] == [r.model_dump() for r in discovery_draft().references]
    assert all(opts["max_retries"] == 0 for opts in provider.client_options)
    for body, extensions in provider.requests:
        assert body["model"] == MODEL_ID and body["max_output_tokens"] == 2048
        assert body["store"] is body["parallel_tool_calls"] is False
        assert extensions["timeout"]["read"] <= 10
    summary = report_markdown(report)
    assert "provider request ceiling: 2" in summary and "exact discovery: True" in summary
    assert "grounding / provenance / authorized scope: True" in summary
    assert json.loads(json.dumps(report)) == report


def test_original_dataset_contract_and_default_plan_are_discovery_only():
    dataset, scenarios = live_evaluation.smoke_scenarios()
    original = next(s for s in load_dataset().scenarios if s.id == "authorized-discovery")
    assert scenarios == (original,)
    plan = execution_plan()
    assert plan["dataset_version"] == dataset.version
    assert plan["execution_mode"] == "discovery-only" and plan["scenario_count"] == 1
    assert plan["scenarios"][0]["expected_discovery_queue_keys"] == list(original.authorized_queue_scope)
    assert plan["scenarios"][0]["expected_discovery_order"] == "queue-id-ascending"
    assert plan["global_max_provider_requests"] == plan["max_provider_requests_per_scenario"] == 2
    assert plan["max_model_turns_per_scenario"] == 2 and plan["max_tool_requests_per_scenario"] == 1
    assert plan["max_output_tokens_per_request"] == 2048 and plan["global_max_output_tokens"] == 4096
    assert plan["provider_requests_executed"] == 0 and not plan["provider_enabled"]


@pytest.mark.parametrize("attack", [
    "premature_answer", "assessment_first", "assessment_second", "repeated_discovery",
    "fabricated_source", "fabricated_pointer", "missing_reference", "reversed_references",
    "malformed_answer", "malformed_arguments", "multiple_calls", "truncation", "hosted_tool",
    "http_first", "http_second", "timeout",
])
def test_failure_is_terminal_without_repair_or_assessment(attack):
    outcomes = script()[:2]
    requests, dispatches = 2, 1
    if attack == "premature_answer":
        outcomes[0] = response(message(discovery_draft()))
        requests, dispatches = 1, 0
    elif attack.startswith("assessment"):
        index = 0 if attack == "assessment_first" else 1
        outcomes[index] = response(call("get_queue_recommendations", '{"queue_id":7}', "assess"))
        requests, dispatches = index + 1, index
    elif attack == "repeated_discovery":
        outcomes[1] = response(call(call_id="again"))
    elif attack.startswith("fabricated"):
        reference = EvidenceReference(source_id="invented" if attack == "fabricated_source" else "eval_1",
                                      kind="queue", pointer="/999" if attack == "fabricated_pointer" else "/0")
        outcomes[1] = response(message(discovery_draft().model_copy(update={"references": (reference,)})))
    elif attack in {"missing_reference", "reversed_references"}:
        refs = discovery_draft().references
        outcomes[1] = response(message(discovery_draft().model_copy(update={
            "references": refs[:-1] if attack == "missing_reference" else refs[::-1],
        })))
    elif attack == "malformed_answer":
        outcomes[1] = response(message(text='{"type":"answer"}'))
    elif attack == "malformed_arguments":
        outcomes[0] = response(call(arguments='{"unexpected":true}'))
        requests, dispatches = 1, 0
    elif attack == "multiple_calls":
        outcomes[0] = response(call(), call(call_id="second"))
        requests, dispatches = 1, 0
    elif attack == "truncation":
        outcomes[1] = response(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    elif attack == "hosted_tool":
        outcomes[0] = response({"type": "web_search_call", "id": "hosted", "status": "completed"})
        requests, dispatches = 1, 0
    elif attack.startswith("http"):
        index = 0 if attack == "http_first" else 1
        outcomes[index] = (503, {"error": {"message": "SECRET"}})
        requests, dispatches = index + 1, index
    else:
        outcomes[0] = httpx.ReadTimeout("SECRET")
        requests, dispatches = 1, 0
    # Extra successful outcomes must never be consumed for retries, repairs or assessments.
    report, provider = discovery(MockProvider(outcomes + script()))
    assert_only_discovery(report)
    assert not report["required_gates_passed"] and report["terminal_outcome"] == "failed"
    assert report["executed_scenario_count"] == report["failed"] == 1 and report["passed"] == 0
    assert report["not_run"] == report["unscored_attempts"] == []
    assert report["provider_request_count"] == len(provider.requests) == requests
    assert report["tool_dispatch_count"] == report["tool_request_count"] == dispatches
    assert report["tool_rejection_count"] == 0  # Protocol attacks stop inside the adapter.
    assert all(e.get("tool") in {None, "list_queues"} for e in report["results"][0]["events"])
    assert report["metrics"]["planned_task_completion_rate"]["rate"] == 0
    assert "SECRET" not in json.dumps(report)
    if attack in {"premature_answer", "fabricated_source", "fabricated_pointer",
                  "missing_reference", "reversed_references"}:
        assert report["results"][0]["error_code"] == "invalid_answer"


def test_two_request_budget_survives_distinct_adapters_and_scopes_and_counts_failures():
    budget = ProviderRequestBudget(2)
    provider = MockProvider([(429, {"error": {"message": "SECRET"}})] * 3)
    request = {"question": "Synthetic discovery", "workflow": "discovery", "queue_name": None}
    for _ in range(4):
        adapter = OpenAIResponsesAdapter(ResponsesSettings(enabled=True, model_id=MODEL_ID, max_requests=2),
                                         client_factory=provider.factory, request_budget=budget)
        with adapter.open_run() as scope:
            turn = scope.next_turn(({"role": "user", "request": request},), ("list_queues",),
                                   StructuredAnswerDraft.model_json_schema(), monotonic() + 30)
            assert turn.type == "model_failure"
    assert budget.used == len(provider.requests) == len(provider.clients) == 2
    assert all(c.is_closed() for c in provider.clients)


def test_lowered_budget_never_attempts_second_request_or_fails_unplanned_work():
    report, provider = discovery(max_provider_requests=1)
    assert_only_discovery(report)
    assert report["provider_request_count"] == len(provider.requests) == 1
    assert report["stop_reason"] == "global_request_budget_exhausted"
    assert report["failed"] == 1 and report["not_run"] == []


@pytest.mark.parametrize("bad", [3, 9, True, 2.0])
def test_discovery_budget_cannot_be_raised_or_coerced(bad):
    with pytest.raises(ValidationError):
        SmokeSettings(max_provider_requests=bad)


def test_unvalidated_settings_cannot_raise_budget(monkeypatch):
    monkeypatch.setattr(evaluation_fixtures, "synthetic_database", lambda: pytest.fail("database"))
    provider = MockProvider(script())
    config = SmokeSettings(provider_enabled=True).model_copy(update={"max_provider_requests": 9})
    report = run_smoke(config, consent(), client_factory=provider.factory)
    assert report["stop_reason"] == "invalid_model_settings"
    assert report["provider_request_count"] == 0 and provider.clients == []
    assert report["global_max_provider_requests"] == 2


def test_missing_consent_blocks_before_credentials_client_or_database(monkeypatch):
    real_get = os.environ.get

    def guarded_get(name, *args):
        assert name != "OPENAI_API_KEY"
        return real_get(name, *args)

    monkeypatch.setattr(os.environ, "get", guarded_get)
    monkeypatch.setattr(evaluation_fixtures, "synthetic_database", lambda: pytest.fail("database"))
    provider = MockProvider(script())
    report = run_smoke(SmokeSettings(provider_enabled=True), client_factory=provider.factory)
    assert_only_discovery(report)
    assert report["terminal_outcome"] == "not_attempted"
    assert report["stop_reason"] == "missing_explicit_consent"
    assert report["executed_scenario_count"] == report["failed"] == report["provider_request_count"] == 0
    assert len(report["not_run"]) == 1 and provider.clients == []
    assert "not run | unexecuted" in report_markdown(report)
    assert not report["security_audit_complete"]


def test_full_suite_has_no_real_transport_path_even_outside_automation(monkeypatch):
    monkeypatch.setattr(live_evaluation, "_automation_context", lambda: False)
    real_get = os.environ.get

    def guarded_get(name, *args):
        assert name != "OPENAI_API_KEY"
        return real_get(name, *args)

    monkeypatch.setattr(os.environ, "get", guarded_get)
    monkeypatch.setattr(evaluation_fixtures, "synthetic_database", lambda: pytest.fail("database"))
    report = run_smoke(SmokeSettings(provider_enabled=True, execution_mode="mocked-suite",
                                    max_provider_requests=9), consent())
    assert report["stop_reason"] == "mocked_suite_requires_mock_transport"
    assert report["provider_request_count"] == report["executed_scenario_count"] == 0


@pytest.mark.parametrize("marker", ["CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST"])
def test_automation_marker_blocks_real_transport_in_fresh_process(marker, tmp_path):
    code = '''
import os
from unittest.mock import patch
from ai_operations_agent.live_evaluation import *
original = os.environ.get
def guarded(name, *args):
    assert name != "OPENAI_API_KEY"
    return original(name, *args)
with patch.object(os.environ, "get", guarded):
    report = run_smoke(SmokeSettings(provider_enabled=True),
                       SmokeConsent(opted_in=True, confirmation=CONFIRMATION))
assert report["stop_reason"] == "automated_live_execution_blocked"
assert report["provider_request_count"] == report["executed_scenario_count"] == 0
'''
    env = {k: v for k, v in os.environ.items()
           if k not in {"CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST", "OPENAI_API_KEY"}}
    env.update({marker: "false", "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                OPT_IN_VARIABLE: OPT_IN_VALUE})
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_confirmation_rejects_old_suite_phrase_and_identifies_discovery_limit(monkeypatch):
    monkeypatch.setattr(live_evaluation, "_automation_context", lambda: False)
    monkeypatch.setenv(OPT_IN_VARIABLE, OPT_IN_VALUE)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with patch.object(builtins, "input", return_value="RUN gpt-6-luna SYNTHETIC SMOKE") as prompt:
        permission, reason = live_evaluation._cli_consent()
    assert permission is None and reason == "missing_explicit_consent"
    text = prompt.call_args.args[0]
    assert "authorized-discovery" in text and "two provider requests" in text and CONFIRMATION in text
    with pytest.raises(ValidationError):
        SmokeConsent(opted_in=True, confirmation="RUN gpt-6-luna SYNTHETIC SMOKE")


@pytest.mark.parametrize("argument", ["--execution-mode", "--scenario", "--max-provider-requests"])
def test_cli_cannot_select_suite_scenarios_or_raise_ceiling(argument, capsys):
    with pytest.raises(SystemExit) as error:
        live_evaluation.main([argument, "SECRET"])
    assert error.value.code == 2 and "SECRET" not in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["score", "cleanup"])
def test_incomplete_audit_retains_attempt_without_inventing_unexecuted_scenarios(monkeypatch, failure):
    def broken_score(*args, **kwargs):
        raise RuntimeError("SECRET")

    original = evaluation_fixtures.synthetic_database

    @contextmanager
    def broken_cleanup():
        with original() as fixtures:
            yield fixtures
        raise RuntimeError("SECRET")

    if failure == "score":
        monkeypatch.setattr(evaluation, "score_scenario", broken_score)
    else:
        monkeypatch.setattr(evaluation_fixtures, "synthetic_database", broken_cleanup)
    report, provider = discovery()
    assert_only_discovery(report)
    assert not report["required_gates_passed"] and not report["security_audit_complete"]
    assert report["terminal_outcome"] == "incomplete_audit"
    assert report["stop_reason"] == "smoke_execution_failed"
    assert report["provider_request_count"] == len(provider.requests) == 2
    assert report["executed_scenario_count"] == 1 and report["not_run"] == []
    assert len(report["unscored_attempts"]) == (1 if failure == "score" else 0)
    assert "SECRET" not in json.dumps(report)


def test_missing_usage_and_price_are_unknown_after_discovery():
    outcomes = script()[:2]
    del outcomes[1]["usage"]
    report, _ = discovery(MockProvider(outcomes))
    assert report["required_gates_passed"] and report["provider_request_count"] == 2
    assert all(v is None for v in report["usage"].values())
    assert report["cost"]["estimated_usd"] is None
    assert "tokens: unknown; estimated USD: unknown" in report_markdown(report)
