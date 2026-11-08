"""Sanitized boundaries through the official SDK, verified MockTransport, private fixtures."""

import json
import os
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from test_assessment_live_evaluation import assessment, assessment_script, settings
from test_discovery_live_evaluation import discovery
from test_live_evaluation import mocked
from test_live_evaluation import (
    no_default_database_or_mutable_runtime_session as no_default_database_or_mutable_runtime_session,
)
from test_live_evaluation import settings as suite_settings
from test_offline_agent_evaluations import focus
from test_openai_responses import MockProvider, message, next_turn
from test_openai_responses import no_provider_network as no_provider_network

from ai_operations_agent import agent_evidence, agent_runtime, evaluation, evaluation_fixtures
from ai_operations_agent.agent_schemas import EvidenceReference
from ai_operations_agent.agent_telemetry import RuntimeTrace
from ai_operations_agent.answer_diagnostics import RejectionReason, sanitized_rejection_reason
from ai_operations_agent.live_evaluation import report_markdown
from ai_operations_agent.openai_responses import OpenAIResponsesAdapter, ResponsesSettings
from ai_operations_agent.provider_budget import ProviderRequestBudget


def candidate(draft):
    def outcomes(ids):
        values = assessment_script(ids)
        # Preserve the existing usage fields and reasoning; neither belongs in diagnostics.
        values[1]["output"][-1] = message(draft)
        return values
    return outcomes


def assert_rejection(report, provider, reason):
    result = report["results"][0]
    assert result["error_code"] == "invalid_answer" and result["status"] == "error"
    assert result["rejection_reason"] == reason.value
    assert report["stop_reason"] == "scenario_gate_failed"
    assert report["terminal_outcome"] == "failed" and not report["required_gates_passed"]
    assert report["request_budget_ceiling_reached"] is True
    assert report["provider_request_prevented"] is False
    assert report["provider_request_count"] == result["provider_request_count"] == len(provider.requests) == 2
    assert report["model_turn_count"] == result["model_turn_count"] == 2
    assert report["tool_request_count"] == report["tool_dispatch_count"] == 1
    assert report["tool_rejection_count"] == 0
    assert report["usage"] == result["usage"] == {
        "input_tokens": 22, "output_tokens": 14, "total_tokens": 36,
    }
    assert report["security_audit_complete"] and report["security_violations"] == []
    assert report["not_run"] == report["unscored_attempts"] == []
    serialized = json.dumps(report, allow_nan=False) + report_markdown(report)
    assert reason.value in serialized
    for forbidden in ("CANDIDATE_SENTINEL", "EXCEPTION_SENTINEL", "mock-provider-sentinel",
                      "encrypted_content", "opaque-mocked-reasoning", "source_id", "pointer",
                      "Synthetic ticket", "Synthetic operator", "Review overdue active tickets"):
        assert forbidden not in serialized
    assert json.loads(json.dumps(report)) == report


@pytest.mark.parametrize(("change", "reason"), [
    ({"kind": "discovery"}, RejectionReason.INVALID_ANSWER_SELECTION),
    ({"inability": "missing_queue"}, RejectionReason.INVALID_ANSWER_SELECTION),
    ({"recommendation_codes": focus().recommendation_codes[:-1]},
     RejectionReason.RECOMMENDATION_SELECTION_MISMATCH),
    ({"recommendation_codes": focus().recommendation_codes[::-1]},
     RejectionReason.RECOMMENDATION_SELECTION_MISMATCH),
    ({"references": focus(source="eval_1").references[::-1]},
     RejectionReason.ASSESSMENT_REFERENCE_LAYOUT_MISMATCH),
    ({"references": (EvidenceReference(source_id="CANDIDATE_SENTINEL", kind="assessment",
                                      pointer="/CANDIDATE_SENTINEL"),)},
     RejectionReason.ASSESSMENT_REFERENCE_LAYOUT_MISMATCH),
    ({"blocks": ()}, RejectionReason.EXPLANATION_BLOCKS_MISMATCH),
    ({"blocks": ("recommendations", "queue_summary")}, RejectionReason.EXPLANATION_BLOCKS_MISMATCH),
    ({"blocks": ("queue_summary", "no_rules")}, RejectionReason.EXPLANATION_BLOCKS_MISMATCH),
])
def test_explicit_rejection_boundaries_and_sanitized_reports(change, reason):
    report, provider = assessment(candidate(focus(source="eval_1").model_copy(update=change)))
    assert_rejection(report, provider, reason)


def test_public_response_contract_failure_has_fixed_reason(monkeypatch):
    original = agent_evidence.validate_response_evidence

    def reject_answer(value):
        if value.status == "answered":
            raise agent_evidence.GroundingError("EXCEPTION_SENTINEL with private candidate")
        return original(value)

    monkeypatch.setattr(agent_evidence, "validate_response_evidence", reject_answer)
    report, provider = assessment()
    assert_rejection(report, provider, RejectionReason.RESPONSE_CONTRACT_VALIDATION_FAILURE)


@pytest.mark.parametrize("error", [ValueError, TypeError, RuntimeError, KeyError])
def test_internal_rendering_exception_uses_fixed_fallback(monkeypatch, error):
    def failed(*args):
        raise error("EXCEPTION_SENTINEL with private data")

    monkeypatch.setattr(agent_runtime, "_render", failed)
    report, provider = assessment()
    assert_rejection(report, provider, RejectionReason.INTERNAL_VALIDATION_RENDERING_FAILURE)


@pytest.mark.parametrize("layout", ["copy_supplied", "mandatory_then_metrics", "metrics_reversed"])
def test_supplied_reference_order_is_unchanged_and_optional_metrics_remain_valid(layout):
    original = MockProvider.handle
    supplied = []

    def handle(provider, request):
        body = json.loads(request.content)
        if not body["tools"]:
            tool = next(json.loads(item["output"]) for item in body["input"]
                        if item.get("type") == "function_call_output")
            supplied.extend(tool["references"])
            refs = tuple(EvidenceReference.model_validate(ref) for ref in supplied)
            mandatory = tuple(ref for ref in refs if ref.kind in {"assessment", "recommendation"})
            metrics = tuple(ref for ref in refs if ref.kind == "metric")
            assert metrics and refs != mandatory + metrics
            chosen = (refs if layout == "copy_supplied" else mandatory +
                      (metrics[::-1] if layout == "metrics_reversed" else metrics))
            draft = focus(source="eval_1").model_copy(update={"references": chosen})
            value = assessment_script({"all": 1})[1]
            value["output"][-1] = message(draft)
            provider.script = iter([value])
        return original(provider, request)

    with patch.object(MockProvider, "handle", handle):
        report, provider = assessment()
    assert [ref["kind"] for ref in supplied][0] == "assessment"
    assert [ref["kind"] for ref in supplied][-4:] == ["recommendation"] * 4
    if layout == "copy_supplied":
        assert_rejection(report, provider, RejectionReason.ASSESSMENT_REFERENCE_LAYOUT_MISMATCH)
    else:
        assert report["required_gates_passed"] and report["results"][0]["rejection_reason"] is None
        assert report["request_budget_ceiling_reached"] and not report["provider_request_prevented"]
        assert len(provider.requests) == 2


@pytest.mark.parametrize("mode", ["discovery", "assessment"])
def test_success_at_exactly_two_requests_is_not_budget_prevented(mode):
    report, provider = discovery() if mode == "discovery" else assessment()
    assert report["provider_request_count"] == len(provider.requests) == 2
    assert report["request_budget_ceiling_reached"] and not report["provider_request_prevented"]
    assert report["stop_reason"] is None and report["required_gates_passed"]
    assert report["results"][0]["rejection_reason"] is None


@pytest.mark.parametrize("mode", ["discovery", "assessment"])
def test_early_adapter_budget_rejection_is_reported_as_prevented(mode):
    report, provider = (discovery(max_provider_requests=1) if mode == "discovery" else
                        assessment(config=settings(max_provider_requests=1)))
    assert report["provider_request_count"] == len(provider.requests) == 1
    assert report["request_budget_ceiling_reached"] and report["provider_request_prevented"]
    assert report["stop_reason"] == "global_request_budget_exhausted"
    assert report["results"][0]["error_code"] == "model_unavailable"
    assert report["results"][0]["rejection_reason"] is None
    assert report["security_audit_complete"]


def test_exhausted_budget_blocks_before_client_creation_or_credential_access():
    budget = ProviderRequestBudget(1)
    assert budget.reserve() and budget.exhausted and not budget.blocked
    provider = MockProvider([])
    adapter = OpenAIResponsesAdapter(
        ResponsesSettings(enabled=True, model_id="mock-model"),
        client_factory=provider.factory, request_budget=budget,
    )

    original_get = os.environ.get

    def no_key(name, *args):
        assert name != "OPENAI_API_KEY", "early budget rejection read credentials"
        return original_get(name, *args)

    with patch("ai_operations_agent.openai_responses.os.environ.get", no_key):
        with adapter.open_run() as scoped:
            assert next_turn(scoped).type == "model_failure"
    assert budget.used == 1 and budget.blocked
    assert provider.clients == provider.requests == []


def test_reservation_race_blocks_without_inventing_an_sdk_request():
    budget = ProviderRequestBudget(1)
    provider = MockProvider([])

    def competing_reservation(**kwargs):
        assert budget.reserve()
        return provider.factory(**kwargs)

    adapter = OpenAIResponsesAdapter(
        ResponsesSettings(enabled=True, model_id="mock-model"),
        client_factory=competing_reservation, request_budget=budget,
    )
    trace = RuntimeTrace()
    adapter._trace = trace
    with adapter.open_run() as scoped:
        assert next_turn(scoped).type == "model_failure"
    assert budget.used == 1 and budget.blocked
    assert trace.provider_usage == [] and provider.requests == []
    assert len(provider.clients) == 1 and provider.clients[0].is_closed()


def test_ceiling_between_scenarios_has_no_invented_prevented_provider_request():
    report, provider = mocked(config=suite_settings(max_provider_requests=2))
    assert report["request_budget_ceiling_reached"] and not report["provider_request_prevented"]
    assert report["stop_reason"] == "global_request_budget_exhausted"
    assert len(report["not_run"]) == 2 and len(provider.requests) == 2
    assert report["passed"] == report["executed_scenario_count"] == 1


@pytest.mark.parametrize("failure", ["security", "incomplete_telemetry", "cleanup", "scoring"])
def test_security_and_incomplete_audit_keep_priority_at_budget_ceiling(monkeypatch, failure):
    if failure == "security":
        original = evaluation.score_scenario

        def score(*args, **kwargs):
            args[4]["write_attempts"] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(evaluation, "score_scenario", score)
    elif failure == "incomplete_telemetry":
        original = RuntimeTrace.event

        def event(self, stage, **kwargs):
            if stage != "tool_accepted":
                original(self, stage, **kwargs)

        monkeypatch.setattr(RuntimeTrace, "event", event)
    elif failure == "cleanup":
        original = evaluation_fixtures.synthetic_database

        @contextmanager
        def database():
            with original() as fixtures:
                yield fixtures
            raise RuntimeError("EXCEPTION_SENTINEL cleanup")

        monkeypatch.setattr(evaluation_fixtures, "synthetic_database", database)
    else:
        def score(*args, **kwargs):
            raise RuntimeError("EXCEPTION_SENTINEL scoring")

        monkeypatch.setattr(evaluation, "score_scenario", score)
    report, provider = assessment(candidate(focus(source="eval_1").model_copy(update={"blocks": ()})))
    assert report["provider_request_count"] == len(provider.requests) == 2
    assert report["request_budget_ceiling_reached"] and not report["provider_request_prevented"]
    assert report["stop_reason"] == ("security_violation" if failure in {
        "security", "incomplete_telemetry",
    } else "smoke_execution_failed")
    assert report["security_audit_complete"] is (failure == "security")
    assert not report["required_gates_passed"]
    entry = (report["unscored_attempts"] if failure == "scoring" else report["results"])[0]
    assert entry["rejection_reason"] == "explanation_blocks_mismatch"
    assert "explanation_blocks_mismatch" in report_markdown(report)
    assert "EXCEPTION_SENTINEL" not in json.dumps(report) + report_markdown(report)


@pytest.mark.parametrize("value", ["EXCEPTION_SENTINEL", "invalid_answer_selection", ValueError("SECRET"), {}])
def test_only_fixed_internal_enum_values_can_be_persisted(value):
    assert sanitized_rejection_reason(value) is None


def test_closed_taxonomy_offline_json_and_markdown():
    assert {reason.value for reason in RejectionReason} == {
        "invalid_answer_selection", "recommendation_selection_mismatch",
        "assessment_reference_layout_mismatch", "explanation_blocks_mismatch",
        "response_contract_validation_failure", "internal_validation_rendering_failure",
    }
    report = evaluation.run_evaluation()
    assert report["passed"] == 26 and report["required_gates_passed"]
    results = {r["scenario_id"]: r for r in report["results"]}
    assert results["fabricated-evidence"]["rejection_reason"] == "assessment_reference_layout_mismatch"
    summary = evaluation.markdown_summary(report)
    assert "Rejection reason" in summary and "assessment_reference_layout_mismatch" in summary
    assert "recommendation_selection_mismatch" in json.dumps(report) + summary
    assert all(r["rejection_reason"] is None for r in report["results"] if r["error_code"] != "invalid_answer")
