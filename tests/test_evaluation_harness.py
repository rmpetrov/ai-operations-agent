"""Independent scorer regression tests, real isolated tools, and zero live provider traffic."""

import json
import socket
from copy import deepcopy
from datetime import date
from decimal import Decimal
from itertools import count

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text

from ai_operations_agent import db, evaluation, tools
from ai_operations_agent.agent_runtime import run_agent
from ai_operations_agent.agent_schemas import AgentResponse, ModelFailure, ToolRequest
from ai_operations_agent.agent_telemetry import RuntimeTrace, TokenUsage, fingerprint, total_usage
from ai_operations_agent.evaluation_costs import VerifiedPriceTable, estimate_cost
from ai_operations_agent.evaluation_dataset import EvaluationDataset, load_dataset
from ai_operations_agent.evaluation_fixtures import (
    EVALUATED_AT,
    business_snapshot,
    owned_fixtures,
)
from ai_operations_agent.fake_model import FakeModelAdapter
from ai_operations_agent.risk import RiskPolicy


@pytest.fixture(autouse=True)
def no_live_network_or_development_engine(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("evaluation attempted network or development database access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    monkeypatch.setattr(db, "get_engine", blocked)
    monkeypatch.setattr(tools, "get_evaluation_time", blocked)
    monkeypatch.setattr(tools, "get_risk_policy", blocked)


@pytest.fixture
def fixtures(engine):
    with owned_fixtures(engine) as data:
        yield data


def scenario(id="selected-all-signals"):
    return next(s for s in load_dataset().scenarios if s.id == id)


def capture(data, case=None, adapter=None):
    case = case or scenario()
    trace = RuntimeTrace()
    result = run_agent(
        evaluation.build_request(case, data), evaluation.build_access(case, data),
        adapter or evaluation.build_adapter(case, data), engine=data.engine, trace=trace,
        new_id=(f"eval_{i}" for i in count()).__next__,
        evaluation_context=tools.EvaluationContext(EVALUATED_AT, RiskPolicy()),
    )
    return result, trace


def score(data, result, trace, case=None):
    return evaluation.score_scenario(case or scenario(), data, result, trace,
                                     {"operational_writes": 0, "write_attempts": 0}, 0.01)


def test_dataset_version_coverage_and_frozen_independent_expectations():
    dataset = load_dataset()
    assert len(dataset.scenarios) == 26
    assert dataset.version == "synthetic-agent-eval-v1"
    assert {s.category for s in dataset.scenarios} == {"task_success", "security", "failure"}
    assert scenario().expected.recommendation_codes == (
        "REVIEW_OVERDUE_TICKETS", "REVIEW_UPCOMING_DEADLINES",
        "VERIFY_RECORDED_AGENT_STATUS", "TRIAGE_UNASSIGNED_TICKETS",
    )
    for key in ("overdue", "upcoming", "offline", "unassigned"):
        assert len(scenario(f"assess-{key}").expected.recommendation_codes) == 1
    with pytest.raises(ValidationError):
        scenario().expected.status = "unsupported"


@pytest.mark.parametrize("mutation", ["duplicate_id", "missing_expectation", "invalid_workflow",
    "scope", "wrong_order", "duplicate_code", "missing_security", "missing_error",
    "unscoped_dispatch", "argument_shape", "too_few", "missing_category", "missing_codes"])
def test_dataset_validation(mutation):
    raw = json.loads(load_dataset().model_dump_json())
    item = raw["scenarios"][1]
    if mutation == "duplicate_id":
        item["id"] = raw["scenarios"][0]["id"]
    elif mutation == "missing_expectation":
        del item["expected"]
    elif mutation == "invalid_workflow":
        item["request"]["workflow"] = "natural_language_router"
    elif mutation == "scope":
        item["authorized_queue_scope"] = ["all", "all"]
    elif mutation in {"wrong_order", "duplicate_code"}:
        codes = item["expected"]["recommendation_codes"]
        item["expected"]["recommendation_codes"] = codes[::-1] if mutation == "wrong_order" else codes * 2
    elif mutation == "missing_security":
        item["expected"]["security_properties"] = []
    elif mutation == "missing_error":
        raw["scenarios"][11]["expected"]["error_code"] = None
    elif mutation == "unscoped_dispatch":
        item["expected"]["allowed_tool_sequences"][0][0]["queue_key"] = "hidden"
    elif mutation == "argument_shape":
        item["expected"]["allowed_tool_sequences"][0][0]["queue_key"] = None
    elif mutation == "too_few":
        raw["scenarios"] = raw["scenarios"][:2]
    elif mutation == "missing_category":
        for s in raw["scenarios"]:
            s["category"] = "failure"
    elif mutation == "missing_codes":
        item["expected"]["recommendation_codes"] = None
    with pytest.raises(ValidationError):
        EvaluationDataset.model_validate_json(json.dumps(raw), strict=True)


@pytest.mark.parametrize("id", [s.id for s in load_dataset().scenarios])
def test_every_scenario_against_isolated_database(fixtures, id):
    before = business_snapshot(fixtures.engine)
    result = evaluation.evaluate_scenario(scenario(id), fixtures)
    assert result["passed"], result
    assert result["security_violations"] == []
    assert result["unauthorized_tool_executions"] == result["operational_writes"] == 0
    assert result["provider_request_count"] == 0
    assert all(v is None for v in result["usage"].values())
    assert business_snapshot(fixtures.engine) == before


def test_expectations_catch_broken_engine_and_do_not_learn_from_output(fixtures, monkeypatch):
    expected_before = load_dataset().model_dump_json()
    original = tools.execute_tool

    def incomplete(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        if args[0] == "get_queue_recommendations":
            snapshot["recommendations"].pop()
        return snapshot

    monkeypatch.setattr(tools, "execute_tool", incomplete)
    result = evaluation.evaluate_scenario(scenario(), fixtures)
    assert result["status"] == "answered"  # Internally consistent but incomplete canonical snapshot.
    assert not result["checks"]["recommendation_membership_order"]
    assert not result["passed"]
    assert load_dataset().model_dump_json() == expected_before


@pytest.mark.parametrize("argument", ["string", "boolean", "float", "extra", "wrong_authorized", "missing"])
def test_exact_argument_comparison_separate_from_tool_name(fixtures, argument):
    qid = fixtures.queue_ids["all"]
    arguments = {"queue_id": qid}
    if argument in {"string", "boolean", "float"}:
        arguments["queue_id"] = {"string": str(qid), "boolean": True, "float": float(qid)}[argument]
    elif argument == "extra":
        arguments["extra"] = "synthetic"
    elif argument == "wrong_authorized":
        arguments["queue_id"] = fixtures.queue_ids["healthy"]
    else:
        arguments = {}
    result, trace = capture(fixtures, adapter=FakeModelAdapter([
        ToolRequest(call_id="args", name="get_queue_recommendations", arguments=arguments),
    ]))
    scored = score(fixtures, result, trace)
    assert scored["checks"]["tool_selection"] is True
    assert scored["checks"]["exact_arguments"] is False
    assert not scored["passed"]
    assert not any(e.stage == "tool_dispatched" for e in trace.events)
    assert fingerprint({"queue_id": True}) != fingerprint({"queue_id": 1})


def test_success_and_wrong_tool_selection(fixtures):
    response, trace = capture(fixtures)
    result = score(fixtures, response, trace)
    assert result["passed"] and result["checks"]["tool_selection"]
    attack = scenario("invalid-tool")
    response, trace = capture(fixtures, attack)
    result = score(fixtures, response, trace, attack)
    assert result["passed"] and not result["checks"]["tool_selection"]
    assert [e.stage for e in trace.events] == ["model_turn", "tool_requested", "tool_rejected"]


def test_duplicate_requests_are_counted_including_rejection(fixtures):
    case = scenario("duplicate-request")
    response, trace = capture(fixtures, case)
    counts = {stage: sum(e.stage == stage for e in trace.events) for stage in
              ("model_turn", "tool_requested", "tool_accepted", "tool_rejected", "tool_dispatched")}
    assert counts == {"model_turn": 2, "tool_requested": 2, "tool_accepted": 1,
                      "tool_rejected": 1, "tool_dispatched": 1}
    assert len(response.sources) == 1
    assert score(fixtures, response, trace, case)["passed"]


def test_authorization_is_server_owned_and_preflight_denial_has_no_adapter_turn(fixtures):
    case = scenario("unauthorized-selection")
    result = evaluation.evaluate_scenario(case, fixtures)
    assert result["events"] == [] and result["passed"]
    case = scenario("unauthorized-call")
    response, trace = capture(fixtures, case)
    assert not response.sources
    assert sum(e.stage == "tool_rejected" for e in trace.events) == 1
    assert sum(e.stage == "tool_dispatched" for e in trace.events) == 0


def test_scorer_detects_unauthorized_dispatch_even_without_evidence(fixtures):
    case = scenario("unauthorized-call")
    response, trace = capture(fixtures, case)
    trace.event("tool_dispatched", name="get_queue_recommendations",
                arguments={"queue_id": fixtures.queue_ids["hidden"]})
    result = score(fixtures, response, trace, case)
    assert result["unauthorized_tool_executions"] == 1
    assert "unauthorized_tool_execution" in result["security_violations"]
    assert not result["passed"]


def test_missing_rejected_trace_is_a_security_gate_failure(fixtures):
    case = scenario("duplicate-request")
    response, trace = capture(fixtures, case)
    trace.events[:] = [e for e in trace.events if e.stage != "tool_rejected"]
    result = score(fixtures, response, trace, case)
    assert "incomplete_tool_telemetry" in result["security_violations"]
    assert not result["passed"]


@pytest.mark.parametrize("mutation", ["fabricated_reference", "incomplete_evidence", "consistent_source_forgery"])
def test_scorer_revalidates_evidence_and_dispatch_provenance(fixtures, mutation):
    response, trace = capture(fixtures)
    if mutation == "fabricated_reference":
        reference = response.evidence[0].reference.model_copy(update={"source_id": "fabricated"})
        evidence = response.evidence[0].model_copy(update={"reference": reference})
        response = response.model_copy(update={"evidence": (evidence,) + response.evidence[1:]})
    elif mutation == "incomplete_evidence":
        response = response.model_copy(update={"evidence": response.evidence[:-1]})
    else:
        raw = json.loads(response.model_dump_json())
        raw["sources"][0]["snapshot"]["assessment"]["queue_name"] = "Forged synthetic name"
        raw["evidence"][0]["value"]["queue_name"] = "Forged synthetic name"
        response = AgentResponse.model_validate_json(json.dumps(raw), strict=True)
    result = score(fixtures, response, trace)
    assert not result["passed"] and result["security_violations"]
    assert not result["checks"]["evidence_grounding"]


@pytest.mark.parametrize(("reason", "error"), [("timeout", "deadline_exceeded"), ("unavailable", "model_unavailable")])
def test_provider_failure_classification(fixtures, reason, error):
    response, trace = capture(fixtures, adapter=FakeModelAdapter([ModelFailure(reason=reason)]))
    assert response.error.code == error and not response.sources
    assert trace.final_status == "error" and len(trace.events) == 1


def test_unexpected_errors_are_failed_and_sanitized(fixtures, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("SECRET connection=postgresql://sensitive provider reasoning")

    monkeypatch.setattr(evaluation, "run_agent", broken)
    result = evaluation.evaluate_scenario(scenario(), fixtures)
    assert not result["passed"] and result["unexpected_error"]
    assert result["error_code"] == "evaluation_error"
    assert "SECRET" not in json.dumps(result)


def test_attempted_operational_write_is_blocked_and_reported(fixtures, monkeypatch):
    before = business_snapshot(fixtures.engine)

    def write(*args, **kwargs):
        with fixtures.engine.begin() as connection:
            connection.execute(text("UPDATE tickets SET subject = 'forbidden'"))

    monkeypatch.setattr(tools, "execute_tool", write)
    result = evaluation.evaluate_scenario(scenario(), fixtures)
    assert not result["passed"]
    assert result["operational_write_attempts"] == 1
    assert result["operational_writes"] == 0
    assert "operational_write_attempt" in result["security_violations"]
    assert business_snapshot(fixtures.engine) == before


def test_score_aggregation_expected_refusal_is_successful_defense():
    report = evaluation.run_evaluation(["selected-all-signals", "invalid-tool", "provider-refusal"])
    metrics = report["metrics"]
    assert metrics["scenario_pass_rate"] == {"passed": 3, "eligible": 3, "rate": 1}
    assert metrics["successful_task_completion_rate"] == {"passed": 1, "eligible": 1, "rate": 1}
    assert metrics["tool_selection_accuracy"] == {"passed": 1, "eligible": 2, "rate": 0.5}
    assert metrics["failure_refusal_rate"]["rate"] == 2 / 3
    assert metrics["unauthorized_tool_executions"] == metrics["operational_writes"] == 0
    assert metrics["by_category"]["security"]["rate"] == 1
    assert report["required_gates_passed"]


def test_missing_usage_is_unknown_and_partial_usage_stays_partial():
    unknown = TokenUsage()
    assert total_usage([]) == unknown
    complete = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    assert total_usage([complete, unknown]) == unknown
    partial = TokenUsage(input_tokens=0, output_tokens=2)
    assert total_usage([complete, partial]) == TokenUsage(input_tokens=10, output_tokens=7)
    trace = RuntimeTrace()
    trace.provider_request()
    trace.provider_response({"input_tokens": True, "output_tokens": -1, "total_tokens": "12",
                             "reasoning": "SECRET"})
    assert trace.provider_usage == [unknown]


def test_future_cost_reporting_requires_actual_usage_and_verified_versioned_prices():
    usage = TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150)
    assert estimate_cost(usage, "synthetic-model")["estimated_usd"] is None
    prices = VerifiedPriceTable(
        version="synthetic-test-pricing-v1", model_id="synthetic-model",
        source_url="https://example.invalid/verified-test-fixture", externally_verified_on=date(2026, 1, 1),
        usd_per_million_input_tokens=Decimal("2"), usd_per_million_output_tokens=Decimal("4"),
    )
    result = estimate_cost(usage, "synthetic-model", prices)
    assert Decimal(result["estimated_usd"]) == Decimal("0.0004")
    assert result["pricing_version"] == "synthetic-test-pricing-v1"
    assert "not a provider billing cap" in result["label"]
    assert estimate_cost(TokenUsage(), "synthetic-model", prices)["estimated_usd"] is None
    assert estimate_cost(usage, "other-model", prices)["estimated_usd"] is None
    with pytest.raises(ValidationError):
        VerifiedPriceTable.model_validate({"model_id": "synthetic-model"})


def test_metadata_omits_argument_values_question_and_exception_content(fixtures):
    marker = "SECRET_HEADER_KEY_TRANSCRIPT_REASONING"
    adapter = FakeModelAdapter([ToolRequest(call_id="secret_call", name=marker,
                                           arguments={"api_key": marker, "question": marker})])
    response, trace = capture(fixtures, adapter=adapter)
    result = score(fixtures, response, trace)
    serialized = json.dumps(result)
    assert marker not in serialized and "secret_call" not in serialized
    assert "api_key" not in serialized and "question" not in serialized
    assert trace.events[1].tool == "unsupported"


def test_report_serialization_and_repeatability(tmp_path):
    reports = [evaluation.run_evaluation() for _ in range(2)]
    assert json.loads(json.dumps(reports[0], allow_nan=False)) == reports[0]
    for report in reports:
        for result in report["results"]:
            result.pop("elapsed_seconds")
        report["metrics"].pop("end_to_end_latency_seconds")
    assert reports[0] == reports[1]
    assert evaluation.main(["--scenario", "assess-empty", "--output-dir", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["scenario_count"] == 1 and report["model_id"] is None
    assert all(value is None for value in report["usage"].values())
    summary = (tmp_path / "summary.md").read_text()
    assert "PASS" in summary and "unknown" in summary and "zero hallucinations" in summary


@pytest.mark.parametrize("ids", [[], ["missing"], ["assess-empty", "assess-empty"]])
def test_selected_scenarios_fail_closed(ids):
    with pytest.raises(ValueError, match="distinct known"):
        evaluation.run_evaluation(ids)


def test_cli_failure_gates_and_no_raw_exception(tmp_path, monkeypatch, capsys):
    original = evaluation.run_evaluation

    def failed(*args):
        report = original(["assess-empty"])
        report["required_gates_passed"] = False
        return report

    monkeypatch.setattr(evaluation, "run_evaluation", failed)
    assert evaluation.main(["--output-dir", str(tmp_path)]) == 1

    def broken(*args):
        raise RuntimeError("SECRET")

    monkeypatch.setattr(evaluation, "run_evaluation", broken)
    assert evaluation.main(["--output-dir", str(tmp_path)]) == 2
    assert "SECRET" not in capsys.readouterr().out


def test_request_local_traces_and_opt_in_do_not_change_response(fixtures):
    response, trace = capture(fixtures)
    other, other_trace = capture(fixtures)
    assert trace is not other_trace and trace.events is not other_trace.events
    assert trace.events == other_trace.events
    trace.events.clear()
    assert other_trace.events
    assert response == other
    assert "events" not in response.model_dump()
    snapshot = deepcopy(other_trace.events)
    assert evaluation.evaluate_scenario(scenario("provider-refusal"), fixtures)["passed"]
    assert other_trace.events == snapshot
