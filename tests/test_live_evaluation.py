"""Controlled smoke preparation: official SDK + MockTransport, never a live request."""

import builtins
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from test_offline_agent_evaluations import focus
from test_openai_responses import MockProvider, call, message, reasoning, response
from test_openai_responses import no_provider_network as no_provider_network

from ai_operations_agent import db, evaluation, live_evaluation, read_only, tools
from ai_operations_agent.agent_schemas import EvidenceReference, StructuredAnswerDraft
from ai_operations_agent.evaluation_costs import VerifiedPriceTable
from ai_operations_agent.evaluation_dataset import load_dataset
from ai_operations_agent.live_evaluation import (
    CONFIRMATION,
    MODEL_ID,
    OPT_IN_VALUE,
    OPT_IN_VARIABLE,
    SCENARIO_IDS,
    SmokeConsent,
    SmokeSettings,
    execution_plan,
    report_markdown,
    run_smoke,
)
from ai_operations_agent.openai_responses import OpenAIResponsesAdapter, ResponsesSettings
from ai_operations_agent.provider_budget import ProviderRequestBudget


def consent():
    return SmokeConsent(opted_in=True, confirmation=CONFIRMATION)


def settings(**overrides):
    return SmokeSettings(provider_enabled=True, **overrides)


def discovery_draft():
    return StructuredAnswerDraft(kind="discovery", blocks=("discovery",), references=tuple(
        EvidenceReference(source_id="eval_1", kind="queue", pointer=f"/{i}") for i in range(2)
    ))


def script():
    # Fixture IDs are deterministic in the private SQLite database, never taken from dev.
    outcomes = [
        response(reasoning("discovery"), call(call_id="discover")),
        response(reasoning("discovered"), message(discovery_draft())),
        response(reasoning("selected"), call("get_queue_recommendations", '{"queue_id":7}', "selected")),
        response(reasoning("selected_answer"), message(focus(source="eval_1"))),
        response(reasoning("named_list"), call(call_id="named_list")),
        response(reasoning("named_assess"), call("get_queue_recommendations", '{"queue_id":2}', "named_assess")),
        response(reasoning("named_answer"), message(focus((), source="eval_2"))),
    ]
    for outcome in outcomes:
        outcome.update(model=MODEL_ID, usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18})
    return outcomes


def mocked(provider=None, config=None, **kwargs):
    provider = provider or MockProvider(script())
    result = run_smoke(config or settings(), consent(), client_factory=provider.factory, **kwargs)
    assert all(client.is_closed() for client in provider.clients)
    return result, provider


@pytest.fixture(autouse=True)
def no_default_database_or_mutable_runtime_session(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("smoke runner used backend configuration or a development database")

    monkeypatch.setattr(db, "get_engine", forbidden)
    monkeypatch.setattr(read_only, "get_engine", forbidden)
    monkeypatch.setattr(tools, "get_evaluation_time", forbidden)
    monkeypatch.setattr(tools, "get_risk_policy", forbidden)


def test_all_three_workflows_real_runtime_scorer_and_usage(monkeypatch):
    original_score = evaluation.score_scenario
    original_runtime = evaluation.run_agent
    with (patch.object(evaluation, "score_scenario", wraps=original_score) as scorer,
          patch.object(evaluation, "run_agent", wraps=original_runtime) as runtime):
        report, provider = mocked()
    assert report["required_gates_passed"], report
    assert report["security_audit_complete"]
    assert report["mode"] == "mocked" and report["model_id"] == MODEL_ID
    assert report["passed"] == report["executed_scenario_count"] == report["scenario_count"] == 3
    assert report["not_run"] == report["unscored_attempts"] == []
    assert report["provider_request_count"] == len(provider.requests) == 7
    assert report["model_turn_count"] == 7
    assert report["tool_request_count"] == report["tool_dispatch_count"] == 4
    assert report["tool_rejection_count"] == 0
    assert report["usage"] == {"input_tokens": 77, "output_tokens": 49, "total_tokens": 126}
    assert report["cost"]["estimated_usd"] is None
    assert scorer.call_count == runtime.call_count == 3
    assert [r["scenario_id"] for r in report["results"]] == list(SCENARIO_IDS)
    assert [r["workflow"] for r in report["results"]] == ["discovery", "queue_assessment", "queue_assessment"]
    assert [r["model_turn_count"] for r in report["results"]] == [2, 2, 3]
    assert all(r["checks"]["tool_selection"] and r["checks"]["exact_arguments"] for r in report["results"])
    assert report["metrics"]["by_category"]["failure"]["eligible"] == 0
    for call_args in runtime.call_args_list:
        assert call_args.kwargs["engine"].dialect.name == "sqlite"
        assert call_args.kwargs["engine"].url.database == ":memory:"
        assert call_args.kwargs["limits"].timeout_seconds == 30
        assert call_args.kwargs["limits"].max_model_turns == 3
        assert call_args.kwargs["limits"].max_tool_requests == 2
    for options in provider.client_options:
        assert options["max_retries"] == 0
        assert options["base_url"] == "https://api.openai.com/v1"
    for body, extensions in provider.requests:
        assert body["model"] == MODEL_ID
        assert body["store"] is body["parallel_tool_calls"] is False
        assert body["max_output_tokens"] == 2048
        assert extensions["timeout"]["read"] <= 10
        assert all(t["type"] == "function" and t["strict"] for t in body["tools"])
        assert all(t["parameters"]["additionalProperties"] is False for t in body["tools"])
        assert "previous_response_id" not in body


def test_realistic_stateless_replay_and_call_output_correlation():
    report, provider = mocked()
    assert report["required_gates_passed"]
    bodies = [body for body, _ in provider.requests]
    for first_index, final_index, expected_ids in [(0, 1, ["discover"]), (2, 3, ["selected"]),
                                                  (4, 6, ["named_list", "named_assess"])]:
        assert len(bodies[first_index]["input"]) == 1
        inputs = bodies[final_index]["input"]
        assert [item["call_id"] for item in inputs if item.get("type") == "function_call_output"] == expected_ids
        assert [item["call_id"] for item in inputs if item.get("type") == "function_call"] == expected_ids
        assert any(item.get("type") == "reasoning" and item["encrypted_content"] for item in inputs)
        for item in inputs:
            if item.get("type") == "function_call_output":
                value = json.loads(item["output"])
                assert value["source"]["run_id"] == "eval_0"
                assert value["references"]
    assert json.loads(next(item["output"] for item in bodies[6]["input"]
                           if item.get("type") == "function_call_output"
                           and item["call_id"] == "named_assess"))["source"]["snapshot"]["assessment"]["queue_id"] == 2


def test_fixed_scenario_expectations_are_original_dataset_objects():
    dataset, scenarios = live_evaluation.smoke_scenarios()
    independent = load_dataset()
    assert [s.id for s in scenarios] == list(SCENARIO_IDS)
    for scenario in scenarios:
        original = next(s for s in independent.scenarios if s.id == scenario.id)
        assert scenario.expected == original.expected
        assert scenario.authorized_queue_scope == original.authorized_queue_scope
    assert execution_plan()["dataset_version"] == dataset.version


def test_dry_run_and_import_read_no_credentials_dotenv_or_provider_in_subprocess(tmp_path):
    root = Path(__file__).resolve().parents[1]
    code = r'''
import builtins, json, os, sys
from unittest.mock import patch
real_import = builtins.__import__
def imports(name, *args, **kwargs):
    if name == "openai" or name in {"ai_operations_agent.config", "ai_operations_agent.openai_responses", "ai_operations_agent.evaluation"}:
        raise AssertionError("provider/backend imported by dry-run")
    return real_import(name, *args, **kwargs)
def audit(event, arguments):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise AssertionError("network")
    if event == "open" and isinstance(arguments[0], (str, bytes)) and os.path.basename(arguments[0]) in {".env", b".env"}:
        raise AssertionError("dotenv read")
sys.addaudithook(audit)
original_get = os.environ.get
original_item = type(os.environ).__getitem__
def get(name, *args):
    if name == "OPENAI_API_KEY": raise AssertionError("credential read")
    return original_get(name, *args)
def item(self, name):
    if name == "OPENAI_API_KEY": raise AssertionError("credential read")
    return original_item(self, name)
with patch.object(builtins, "__import__", imports), patch.object(os.environ, "get", get), patch.object(type(os.environ), "__getitem__", item):
    from ai_operations_agent import live_evaluation
    assert live_evaluation.main([]) == 0
    assert "ai_operations_agent.config" not in sys.modules
    assert "openai" not in sys.modules
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(root / "src"),
                                 "DATABASE_URL": "postgresql+psycopg://development.invalid/persistent",
                                 "TEST_DATABASE_URL": "postgresql+psycopg://test.invalid/persistent",
                                 OPT_IN_VARIABLE: OPT_IN_VALUE,
                                 "OPENAI_BASE_URL": "https://forbidden.invalid"})
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["mode"] == "offline-dry-run" and plan["provider_requests_executed"] == 0
    assert plan["model_id"] == MODEL_ID and not plan["provider_enabled"]
    assert not list(tmp_path.iterdir())


def test_dry_plan_budgets_and_no_artifact_creation(tmp_path, capsys):
    assert live_evaluation.main(["--dry-run", "--output-dir", str(tmp_path / "unused")]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["global_max_provider_requests"] == 9
    assert plan["max_output_tokens_per_request"] == 2048
    assert plan["global_max_output_tokens"] == 18432
    assert plan["input_token_budget"] is None
    assert plan["max_model_turns_per_scenario"] == 3 and plan["max_tool_requests_per_scenario"] == 2
    assert plan["scenario_timeout_seconds"] == 30 and plan["request_timeout_seconds"] == 10
    assert all(v is None for v in plan["usage"].values())
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("mutation,reason", [
    ("consent", "missing_explicit_consent"), ("disabled", "provider_disabled"),
    ("model", "invalid_model_settings"), ("key", "missing_api_key"),
])
def test_missing_preconditions_fail_closed_before_client_or_database(monkeypatch, mutation, reason):
    provider = MockProvider(script())
    config, permission = settings(), consent()
    if mutation == "consent":
        permission = None
    elif mutation == "disabled":
        config = SmokeSettings()
    elif mutation == "model":
        config = config.model_copy(update={"model_id": "SECRET_UNSUPPORTED_MODEL"})
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    report = run_smoke(config, permission, client_factory=provider.factory)
    assert report["stop_reason"] == reason and not report["required_gates_passed"]
    assert report["provider_request_count"] == report["executed_scenario_count"] == 0
    assert len(report["not_run"]) == 3
    assert provider.client_options == provider.requests == []
    assert "SECRET" not in json.dumps(report)


def test_pytest_cannot_activate_real_client_even_with_consent(monkeypatch):
    original_get = os.environ.get

    def no_key(name, *args):
        assert name != "OPENAI_API_KEY"
        return original_get(name, *args)

    monkeypatch.setattr(os.environ, "get", no_key)
    report = run_smoke(settings(), consent())
    assert report["stop_reason"] == "automated_live_execution_blocked"
    assert report["provider_request_count"] == 0


def test_cli_live_blocked_under_tests_without_prompt_or_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(OPT_IN_VARIABLE, OPT_IN_VALUE)
    monkeypatch.setattr(builtins, "input", lambda *args: pytest.fail("pytest live prompt"))
    original_get = os.environ.get

    def guarded_get(name, *args):
        assert name != "OPENAI_API_KEY"
        return original_get(name, *args)

    monkeypatch.setattr(os.environ, "get", guarded_get)
    assert live_evaluation.main(["--live", "--output-dir", str(tmp_path)]) == 1
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["stop_reason"] == "automated_live_execution_blocked"
    assert report["provider_request_count"] == 0 and len(report["not_run"]) == 3
    assert "mock-provider-sentinel" not in capsys.readouterr().out


@pytest.mark.parametrize("case", ["missing_opt_in", "not_tty", "wrong_confirmation", "confirmed"])
def test_interactive_consent_mechanism_only_no_execution(monkeypatch, case):
    monkeypatch.setattr(live_evaluation, "_automation_context", lambda: False)
    monkeypatch.setenv(OPT_IN_VARIABLE, OPT_IN_VALUE)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: case != "not_tty")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda *args: CONFIRMATION if case == "confirmed" else "no")
    if case == "missing_opt_in":
        monkeypatch.delenv(OPT_IN_VARIABLE)
    granted, reason = live_evaluation._cli_consent()
    assert (granted == consent() and reason is None) if case == "confirmed" else granted is None and reason


@pytest.mark.parametrize("field,value", [("model_id", "arbitrary-model"), ("model_id", ""),
    ("max_provider_requests", 10), ("max_provider_requests", 0), ("max_provider_requests", True),
    ("max_output_tokens", 2049), ("max_output_tokens", 0),
    ("scenario_timeout_seconds", 31), ("scenario_timeout_seconds", 10.0),
    ("request_timeout_seconds", 11)])
def test_settings_hard_bounds(field, value):
    with pytest.raises(ValidationError):
        SmokeSettings(**{field: value})


def test_consent_rejects_numeric_boolean():
    with pytest.raises(ValidationError):
        SmokeConsent(opted_in=1, confirmation=CONFIRMATION)


def test_mocked_execution_rejects_accidentally_supplied_real_transport():
    openai = pytest.importorskip("openai")
    clients = []

    def factory(**kwargs):
        client = openai.OpenAI(**kwargs, http_client=httpx.Client(trust_env=False))
        clients.append(client)
        return client

    report = run_smoke(settings(), consent(), client_factory=factory)
    assert not report["required_gates_passed"]
    assert report["provider_request_count"] == 0
    assert report["results"][0]["error_code"] == "model_unavailable"
    assert clients and all(client.is_closed() for client in clients)


@pytest.mark.parametrize("argument", ["--api-key", "--model", "--base-url", "--queue-id", "--allowed-queue-ids"])
def test_no_credential_model_url_or_permission_arguments(argument, capsys):
    with pytest.raises(SystemExit) as exc:
        live_evaluation.main([argument, "SECRET_VALUE"])
    assert exc.value.code == 2
    assert "SECRET_VALUE" not in capsys.readouterr().err


def test_global_budget_applies_across_scenarios_and_stops_without_rerun():
    report, provider = mocked(config=settings(max_provider_requests=3))
    assert report["provider_request_count"] == len(provider.requests) == 3
    assert report["passed"] == 1 and report["failed"] == 1
    assert report["stop_reason"] == "global_request_budget_exhausted"
    assert [r["scenario_id"] for r in report["not_run"]] == ["named-assessment"]
    assert report["metrics"]["planned_task_completion_rate"]["rate"] == 1 / 3
    assert not report["required_gates_passed"]


def test_budget_reached_between_scenarios_does_not_create_next_client():
    report, provider = mocked(config=settings(max_provider_requests=2))
    assert len(provider.clients) == 1 and len(provider.requests) == 2
    assert report["executed_scenario_count"] == 1 and len(report["not_run"]) == 2
    assert report["stop_reason"] == "global_request_budget_exhausted"
    assert not report["required_gates_passed"]


def test_budget_hard_ceiling_and_atomic_reservations():
    budget = ProviderRequestBudget()
    with ThreadPoolExecutor(max_workers=4) as pool:
        reservations = list(pool.map(lambda _: budget.reserve(), range(20)))
    assert sum(reservations) == budget.used == 9 and budget.exhausted and budget.blocked
    for invalid in (0, 10, True, 1.5):
        with pytest.raises(ValueError):
            ProviderRequestBudget(invalid)


def test_shared_adapter_budget_counts_failed_requests_across_scopes():
    provider = MockProvider([(503, {"error": {"message": "SECRET"}})] * 3)
    budget = ProviderRequestBudget(3)
    adapter = OpenAIResponsesAdapter(ResponsesSettings(enabled=True, model_id=MODEL_ID),
                                     client_factory=provider.factory, request_budget=budget)
    from time import monotonic

    for _ in range(4):
        with adapter.open_run() as scoped:
            turn = scoped.next_turn(({"role": "user", "request": {
                "question": "Synthetic discovery", "workflow": "discovery", "queue_name": None,
            }},), ("list_queues",), StructuredAnswerDraft.model_json_schema(), monotonic() + 30)
            assert turn.type == "model_failure"
    assert len(provider.requests) == budget.used == 3
    assert len(provider.clients) == 3 and all(client.is_closed() for client in provider.clients)


@pytest.mark.parametrize("kind", ["authentication", "rate_limit", "timeout", "malformed_arguments",
    "extra_function_calls", "truncation"])
def test_provider_failures_classify_and_stop_without_recovery(kind):
    bad = {
        "authentication": (401, {"error": {"message": "SECRET", "type": "authentication_error"}}),
        "rate_limit": (429, {"error": {"message": "SECRET", "type": "rate_limit_error"}}),
        "timeout": httpx.ReadTimeout("SECRET key authorization connection reasoning"),
        "malformed_arguments": response(call(arguments='{"extra":"SECRET"}')),
        "extra_function_calls": response(call(), call(call_id="second")),
        "truncation": response(status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
    }[kind]
    report, provider = mocked(MockProvider([bad]))
    assert not report["required_gates_passed"] and report["provider_request_count"] == 1
    assert len(provider.requests) == len(provider.clients) == 1
    assert report["results"][0]["error_code"] == ("deadline_exceeded" if kind == "timeout" else "model_unavailable")
    assert len(report["not_run"]) == 2
    assert all(value is None for value in report["usage"].values())
    assert "SECRET" not in json.dumps(report) + report_markdown(report)


@pytest.mark.parametrize("qid", [2, 999])
def test_incorrect_or_unauthorized_queue_id_rejected_before_dispatch(qid):
    outcomes = script()[:2] + [response(call("get_queue_recommendations", json.dumps({"queue_id": qid}), "wrong"))]
    report, _ = mocked(MockProvider(outcomes))
    case = report["results"][1]
    assert case["status"] == "error" and case["error_code"] == "queue_unavailable"
    assert any(e["stage"] == "tool_rejected" for e in case["events"])
    assert not any(e["stage"] == "tool_dispatched" for e in case["events"])
    assert case["unauthorized_tool_executions"] == 0 and not report["required_gates_passed"]


@pytest.mark.parametrize("kind", ["fabricated_evidence", "incomplete_recommendations", "reordered_recommendations"])
def test_answer_grounding_and_recommendation_attacks_still_fail(kind):
    outcomes = script()
    if kind == "fabricated_evidence":
        draft = discovery_draft().model_copy(update={"references": (EvidenceReference(
            source_id="fabricated", kind="queue", pointer="/0"),)})
        outcomes[1] = response(message(draft))
    else:
        draft = focus(source="eval_1")
        draft = draft.model_copy(update={"recommendation_codes": draft.recommendation_codes[:-1]
                                        if kind == "incomplete_recommendations" else draft.recommendation_codes[::-1]})
        outcomes[3] = response(message(draft))
    report, _ = mocked(MockProvider(outcomes))
    assert not report["required_gates_passed"]
    assert report["results"][-1]["error_code"] == "invalid_answer"
    assert report["stop_reason"] == "scenario_gate_failed"
    assert report["provider_request_count"] == (2 if kind == "fabricated_evidence" else 4)


def test_expired_deadline_never_creates_client():
    ticks = iter([0.0, 0.0])
    report, provider = mocked(clock=lambda: next(ticks, 31.0))
    assert report["provider_request_count"] == 0 and provider.clients == []
    assert report["results"][0]["error_code"] == "deadline_exceeded"
    assert not report["required_gates_passed"]


def test_late_response_fails_but_actual_usage_is_retained():
    now = [0.0]
    provider = MockProvider(script())
    original = provider.handle

    def delayed(request):
        reply = original(request)
        now[0] = 31.0
        return reply

    provider.handle = delayed
    report, _ = mocked(provider, clock=lambda: now[0])
    assert report["provider_request_count"] == 1
    assert report["usage"]["total_tokens"] == 18
    assert report["results"][0]["error_code"] == "deadline_exceeded"


def test_request_timeout_uses_remaining_runtime_budget():
    ticks = iter([0.0, 0.0])
    report, provider = mocked(config=settings(scenario_timeout_seconds=10),
                              clock=lambda: next(ticks, 9.0))
    assert report["required_gates_passed"]
    assert provider.requests[0][1]["timeout"]["read"] == 1.0
    assert provider.client_options[0]["timeout"] == 1.0


def test_no_operational_writes_and_cleanup_on_attempt(monkeypatch):
    def write(name, arguments, access, *, engine, **kwargs):
        assert engine.url.database == ":memory:"
        with engine.begin() as connection:
            connection.execute(text("UPDATE tickets SET subject = 'forbidden'"))

    monkeypatch.setattr(tools, "execute_tool", write)
    report, provider = mocked()
    assert not report["required_gates_passed"]
    assert report["stop_reason"] == "security_violation"
    assert report["provider_request_count"] == len(provider.requests) == 1
    assert report["results"][0]["operational_writes"] == 0
    assert report["results"][0]["operational_write_attempts"] == 1
    assert any(v["violation"] == "operational_write_attempt" for v in report["security_violations"])


def test_no_development_engine_even_with_database_environment_configured(monkeypatch):
    from ai_operations_agent import evaluation_fixtures

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://development.invalid/operations")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://test.invalid/persistent_test")
    original = evaluation_fixtures.create_engine

    def only_memory(url, **kwargs):
        assert url == "sqlite+pysqlite:///:memory:"
        return original(url, **kwargs)

    with patch.object(evaluation_fixtures, "create_engine", side_effect=only_memory) as engines:
        report, _ = mocked()
    assert report["required_gates_passed"] and engines.call_count == 1


def test_scorer_exception_retains_actual_attempt_and_closes_resources(monkeypatch):
    def failed_score(*args, **kwargs):
        raise RuntimeError("SECRET reasoning transcript")

    monkeypatch.setattr(evaluation, "score_scenario", failed_score)
    report, _ = mocked()
    assert not report["required_gates_passed"] and report["stop_reason"] == "smoke_execution_failed"
    assert report["provider_request_count"] == 2
    assert report["executed_scenario_count"] == 1 and len(report["unscored_attempts"]) == 1
    assert report["unscored_attempts"][0]["provider_request_count"] == 2
    assert len(report["not_run"]) == 2 and report["passed"] == 0
    assert "SECRET" not in json.dumps(report)
    assert not report["security_audit_complete"]


def test_cleanup_failure_cannot_be_reported_as_success(monkeypatch):
    from ai_operations_agent import evaluation_fixtures

    original = evaluation_fixtures.synthetic_database

    @contextmanager
    def failed_cleanup():
        with original() as fixtures:
            yield fixtures
        raise RuntimeError("SECRET cleanup details")

    monkeypatch.setattr(evaluation_fixtures, "synthetic_database", failed_cleanup)
    report, _ = mocked()
    assert report["passed"] == 3 and report["provider_request_count"] == 7
    assert report["stop_reason"] == "smoke_execution_failed" and not report["required_gates_passed"]
    assert not report["security_audit_complete"]


@pytest.mark.parametrize("boundary", ["arguments", "provider_payload"])
def test_smoke_preserves_strict_payload_limits(boundary):
    bad = (response(call(arguments=json.dumps({"padding": "x" * 4096})))
           if boundary == "arguments" else response(message(text="x" * 196608)))
    report, provider = mocked(MockProvider([bad]))
    assert report["provider_request_count"] == len(provider.requests) == 1
    assert report["results"][0]["error_code"] == "model_unavailable"
    assert not report["required_gates_passed"]
    assert report["tool_dispatch_count"] == 0


def test_provider_base_url_environment_cannot_redirect_smoke(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://forbidden.invalid/private")
    report, provider = mocked()
    assert report["required_gates_passed"]
    assert all(options["base_url"] == "https://api.openai.com/v1" for options in provider.client_options)


def test_usage_unknown_if_any_request_lacks_usage_and_reports_sanitized():
    outcomes = script()
    del outcomes[1]["usage"]
    report, _ = mocked(MockProvider(outcomes))
    assert report["required_gates_passed"]
    assert all(value is None for value in report["usage"].values())
    serialized = json.dumps(report, allow_nan=False) + report_markdown(report)
    for secret in ("mock-provider-sentinel", "opaque-mocked-reasoning", "encrypted_content",
                   "Synthetic ticket", "Synthetic operator", "Review the configured synthetic queue."):
        assert secret not in serialized
    assert json.loads(json.dumps(report)) == report
    assert all(r["elapsed_seconds"] >= 0 for r in report["results"])
    assert report["elapsed_seconds"] >= sum(r["elapsed_seconds"] for r in report["results"])


def test_cost_estimate_requires_valid_versioned_table_before_execution():
    provider = MockProvider(script())
    report = run_smoke(settings(), consent(), client_factory=provider.factory, prices="SECRET_UNVERIFIED")
    assert report["stop_reason"] == "invalid_price_table"
    assert report["provider_request_count"] == 0 and provider.clients == []
    assert report["cost"]["estimated_usd"] is None and "SECRET" not in json.dumps(report)


def test_cost_estimate_uses_actual_mock_usage_and_explicit_synthetic_price_fixture():
    prices = VerifiedPriceTable(
        version="synthetic-test-prices-only-v1", model_id=MODEL_ID,
        source_url="https://example.invalid/synthetic-price-fixture", externally_verified_on=date(2026, 1, 1),
        usd_per_million_input_tokens=Decimal("2"), usd_per_million_output_tokens=Decimal("4"),
    )
    report, _ = mocked(prices=prices)
    assert report["required_gates_passed"]
    assert Decimal(report["cost"]["estimated_usd"]) == Decimal("0.00035")
    assert report["cost"]["pricing_version"] == prices.version
    assert "not a provider billing cap" in report["cost"]["label"]


def test_repeated_mocked_runs_get_independent_budgets_clients_and_stable_scores():
    reports = [mocked()[0] for _ in range(2)]
    assert all(r["required_gates_passed"] and r["provider_request_count"] == 7 for r in reports)
    for report in reports:
        report.pop("elapsed_seconds")
        report["metrics"].pop("end_to_end_latency_seconds")
        for result in report["results"]:
            result.pop("elapsed_seconds")
    assert reports[0] == reports[1]
