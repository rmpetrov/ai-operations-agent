"""Reuse the existing official SDK MockTransport adapter; no real provider or credentials."""

import json

import httpx
import pytest
from test_openai_responses import MockProvider, call, message, response
from test_openai_responses import no_provider_network as no_provider_network

from ai_operations_agent.agent_runtime import run_agent
from ai_operations_agent.agent_schemas import EvidenceReference, StructuredAnswerDraft
from ai_operations_agent.agent_telemetry import RuntimeTrace, TokenUsage, total_usage
from ai_operations_agent.evaluation import build_access, build_request, score_scenario
from ai_operations_agent.evaluation_dataset import load_dataset
from ai_operations_agent.evaluation_fixtures import EVALUATED_AT, owned_fixtures
from ai_operations_agent.risk import RiskPolicy
from ai_operations_agent.tools import EvaluationContext


@pytest.mark.parametrize("usage", [None, {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}])
def test_mocked_responses_adapter_usage_and_independent_scoring(engine, usage):
    trace = RuntimeTrace()
    case = next(s for s in load_dataset().scenarios if s.id == "authorized-discovery")
    answer = StructuredAnswerDraft(kind="discovery", blocks=("discovery",), references=tuple(
        EvidenceReference(source_id="source", kind="queue", pointer=f"/{i}") for i in range(2)
    ))
    extra = {"usage": usage} if usage is not None else {}
    provider = MockProvider([response(call(), **extra), response(message(answer), **extra)])
    with owned_fixtures(engine) as fixtures:
        ids = iter(["run", "source"])
        result = run_agent(
            build_request(case, fixtures), build_access(case, fixtures), provider.adapter(trace=trace),
            engine=engine, trace=trace, new_id=lambda: next(ids),
            evaluation_context=EvaluationContext(EVALUATED_AT, RiskPolicy()),
        )
        scored = score_scenario(case, fixtures, result, trace,
                                {"write_attempts": 0, "operational_writes": 0}, 0.01)
    assert scored["passed"]
    assert scored["provider_request_count"] == len(provider.requests) == 2
    assert scored["usage"] == (TokenUsage(input_tokens=22, output_tokens=14, total_tokens=36)
                                if usage else TokenUsage()).model_dump()
    assert all(client.is_closed() for client in provider.clients)
    assert "mock-provider-sentinel" not in json.dumps(scored)


@pytest.mark.parametrize("failure", ["timeout", "failure", "malformed"])
def test_mocked_provider_failures_keep_missing_usage_unknown(engine, failure):
    trace = RuntimeTrace()
    outcome = {"timeout": httpx.ReadTimeout("SECRET encrypted reasoning headers api_key"),
               "failure": (503, {"error": {"message": "SECRET", "type": "unavailable"}}),
               "malformed": response({"type": "unexpected", "private": "SECRET"})}[failure]
    provider = MockProvider([outcome])
    case = next(s for s in load_dataset().scenarios if s.id == "authorized-discovery")
    with owned_fixtures(engine) as fixtures:
        result = run_agent(build_request(case, fixtures), build_access(case, fixtures),
                           provider.adapter(trace=trace), engine=engine, trace=trace)
    assert result.error.code == ("deadline_exceeded" if failure == "timeout" else "model_unavailable")
    assert len(trace.provider_usage) == 1
    assert total_usage(trace.provider_usage) == TokenUsage()
    assert "SECRET" not in result.model_dump_json()
    assert "SECRET" not in json.dumps([event.model_dump() for event in trace.events])


def test_provider_metadata_is_strictly_allowlisted_and_survives_cleanup():
    trace = RuntimeTrace()
    trace.provider_request()
    trace.provider_response({"input_tokens": 12, "output_tokens": 0, "total_tokens": 12,
                             "reasoning_tokens": 99, "encrypted_content": "SECRET",
                             "authorization": "SECRET", "api_key": "SECRET"})
    assert trace.provider_usage == [TokenUsage(input_tokens=12, output_tokens=0, total_tokens=12)]
    assert "SECRET" not in str(trace)
