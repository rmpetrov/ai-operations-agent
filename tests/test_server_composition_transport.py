"""Official SDK MockTransport checks; final model selection is NOT EXERCISED."""

import json
from copy import deepcopy
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy.orm import Session
from test_openai_responses import MockProvider, call, message, reasoning, response
from test_openai_responses import no_provider_network as no_provider_network
from test_server_composition import SERVER, assert_error, run, scenario
from test_server_composition import data as data
from test_server_composition import no_development_database as no_development_database

from ai_operations_agent import evaluation
from ai_operations_agent.agent_schemas import RuntimeLimits, StructuredAnswerDraft
from ai_operations_agent.agent_telemetry import RuntimeTrace, TokenUsage
from ai_operations_agent.answer_composition import CompositionStrategy
from ai_operations_agent.evaluation_costs import estimate_cost
from ai_operations_agent.main import app


@pytest.mark.parametrize(("name", "expected_requests"), [
    ("authorized-discovery", 1), ("selected-all-signals", 1), ("named-assessment", 2),
])
def test_sdk_success_omits_final_request_and_preserves_continuation(data, name, expected_requests):
    case = scenario(name)
    if name == "authorized-discovery":
        script = [response(reasoning(), call())]
    else:
        queue = "healthy" if name == "named-assessment" else "all"
        script = ([response(reasoning(), call())] if name == "named-assessment" else []) + [
            response(call("get_queue_recommendations", json.dumps({"queue_id": data.queue_ids[queue]}),
                          "assess")),
        ]
    provider = MockProvider(script)
    trace = RuntimeTrace()
    # A terminal continuation has no mock response and would fail; count assertions also
    # catch attempts swallowed by the adapter's sanitized failure boundary.
    with patch.object(Session, "flush", side_effect=AssertionError("runtime flush")), \
            patch.object(Session, "commit", side_effect=AssertionError("runtime commit")):
        result, _, trace = run(data, case=case, adapter=provider.adapter(trace=trace), trace=trace)
    assert result.status == "answered"
    assert len(provider.requests) == len(trace.provider_usage) == expected_requests
    assert sum(e.stage == "model_turn" for e in trace.events) == expected_requests
    assert all(client.is_closed() for client in provider.clients)
    assert not trace.final_model_selection_exercised
    assert all(v is None for v in trace.provider_usage[-1].model_dump().values())
    assert estimate_cost(trace.provider_usage[-1], None)["estimated_usd"] is None
    if name == "named-assessment":
        second = provider.requests[1][0]
        assert second["input"][1:3] == [reasoning(), call()]
        output = second["input"][3]
        assert output["type"] == "function_call_output" and output["call_id"] == "call_discovery"
        assert json.loads(output["output"])["source"] == result.sources[0].model_dump(mode="json")
        assert [tool["name"] for tool in second["tools"]] == ["get_queue_recommendations"]


@pytest.mark.parametrize("raw", [
    '{"queue_id":true}', '{"queue_id":"1"}', '{"queue_id":1.0}',
    '{"queue_id":1,"queue_id":1}', '{"queue_id":1,"extra":1}', '{}',
])
def test_sdk_invalid_arguments_never_dispatch_or_compose(data, raw):
    provider = MockProvider([response(call("get_queue_recommendations", raw))])
    result, _, trace = run(data, adapter=provider.adapter())
    assert_error(result, "model_unavailable")
    assert not result.sources and not any(e.stage == "tool_dispatched" for e in trace.events)
    assert len(provider.requests) == 1 and all(client.is_closed() for client in provider.clients)


@pytest.mark.parametrize(("outcome", "expected"), [
    (response(message(StructuredAnswerDraft(kind="queue_focus"))), "invalid_answer"),
    (response(message(text="{}")), "model_unavailable"),
    (response(status="incomplete"), "model_unavailable"),
    (response(call("get_queue_recommendations", '{"queue_id":1}'), message(text="{}")), "model_unavailable"),
    ((429, {"error": {"message": "PRIVATE_SENTINEL", "type": "rate_limit_error"}}), "model_unavailable"),
    (httpx.ReadTimeout("PRIVATE_SENTINEL"), "deadline_exceeded"),
])
def test_sdk_failures_remain_terminal_and_clients_close(data, outcome, expected):
    provider = MockProvider([outcome])
    result, _, _ = run(data, adapter=provider.adapter())
    assert_error(result, expected)
    assert len(provider.requests) == 1 and not result.sources
    assert all(client.is_closed() for client in provider.clients)


def test_sdk_refusal_is_not_replaced_by_server_answer(data):
    provider = MockProvider([response(message(content=[{"type": "refusal", "refusal": "PRIVATE_SENTINEL"}]))])
    result, _, _ = run(data, adapter=provider.adapter())
    assert result.status == "unsupported" and result.uncertainty == ("model_refusal",)
    assert not result.sources and "PRIVATE_SENTINEL" not in result.model_dump_json()
    assert len(provider.requests) == 1 and all(client.is_closed() for client in provider.clients)


def test_sdk_named_continuation_rejects_duplicate_id_without_assessing(data):
    provider = MockProvider([
        response(call()), response(call("get_queue_recommendations",
                                       json.dumps({"queue_id": data.queue_ids["healthy"]}))),
    ])
    result, _, trace = run(data, case=scenario("named-assessment"), adapter=provider.adapter())
    assert_error(result, "model_unavailable")  # Adapter rejects duplicate native call ID.
    assert len(result.sources) == 1 and len(provider.requests) == 2
    assert sum(e.stage == "tool_dispatched" for e in trace.events) == 1
    assert all(client.is_closed() for client in provider.clients)


@pytest.mark.parametrize("failure", ["tool", "render", "size"])
def test_sdk_cleanup_on_postproposal_failure(data, monkeypatch, failure):
    from ai_operations_agent import agent_runtime, tools

    if failure == "tool":
        def unavailable(*args, **kwargs):
            raise tools.ToolError("database unavailable")

        monkeypatch.setattr(tools, "execute_tool", unavailable)
    elif failure == "render":
        def invalid(*args):
            raise ValueError("PRIVATE_SENTINEL")

        monkeypatch.setattr(agent_runtime, "_render", invalid)
    provider = MockProvider([response(call("get_queue_recommendations",
                                           json.dumps({"queue_id": data.queue_ids["all"]})))])
    result, _, _ = run(data, adapter=provider.adapter(),
                       **({"limits": RuntimeLimits(max_response_bytes=1024)} if failure == "size" else {}))
    assert_error(result, {"tool": "database_unavailable", "render": "invalid_answer",
                          "size": "size_limit"}[failure])
    assert len(provider.requests) == 1 and all(client.is_closed() for client in provider.clients)


def test_new_evaluation_metadata_does_not_claim_model_selection_or_usage(data):
    baseline = evaluation.run_evaluation(["selected-all-signals"])
    legacy = baseline["results"][0]
    assert legacy["composition_strategy"] == "model_selected"
    assert legacy["final_model_selection"] == "exercised"
    result = evaluation.evaluate_scenario(scenario(), data, composition_strategy=SERVER)
    assert result["passed"] and result["final_model_selection"] == "not_exercised"
    assert result["composition_strategy"] == "server_owned" and result["provider_request_count"] == 0
    assert result["usage"] == {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    report = deepcopy(baseline)
    report["results"] = [result]
    summary = evaluation.markdown_summary(report)
    assert "server_owned" in summary and "NOT EXERCISED" in summary and "unknown" in summary
    assert "NOT EXERCISED" not in evaluation.markdown_summary(baseline)
    serialized = json.dumps(result) + summary
    for forbidden in ("source_id", "pointer", "encrypted_content", "mock-provider-sentinel",
                      "Synthetic ticket", "Synthetic operator", "PRIVATE_SENTINEL"):
        assert forbidden not in serialized
    assert baseline["cost"]["estimated_usd"] is None


def test_partial_mocked_usage_stays_unknown(data):
    trace = RuntimeTrace()
    provider = MockProvider([
        response(call(), usage={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}),
        response(call("get_queue_recommendations", json.dumps({"queue_id": data.queue_ids["healthy"]}),
                      "assess")),
    ])
    result = evaluation.evaluate_scenario(scenario("named-assessment"), data,
                                           adapter=provider.adapter(trace=trace), trace=trace,
                                           composition_strategy=SERVER)
    assert result["passed"] and result["provider_request_count"] == 2
    assert all(value is None for value in result["usage"].values())
    assert estimate_cost(TokenUsage(**result["usage"]), None)["estimated_usd"] is None


def test_default_evaluation_and_live_runner_keep_legacy_selection():
    from inspect import signature

    from ai_operations_agent.agent_runtime import run_agent
    from ai_operations_agent.live_evaluation import execution_plan

    assert signature(run_agent).parameters["composition_strategy"].default is CompositionStrategy.MODEL_SELECTED
    plan = execution_plan()
    assert plan["max_model_turns_per_scenario"] == 2
    assert plan["max_provider_requests_per_scenario"] == 2
    assert plan["execution_mode"] == "discovery-only"


def test_http_contract_and_provider_laziness_are_unchanged(client, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("HTTP route attempted agent/provider use")

    from ai_operations_agent import agent_runtime, openai_responses

    monkeypatch.setattr(agent_runtime, "run_agent", forbidden)
    monkeypatch.setattr(openai_responses.OpenAIResponsesAdapter, "next_turn", forbidden)
    assert client.get("/health/live").status_code == 200
    assert client.get("/api/v1/queues").status_code == 200
    assert not any("agent" in route.path for route in app.routes
                   if route.path.startswith("/api/") and route.path != "/api/v1/agents")
