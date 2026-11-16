"""Separate official SDK MockTransport accounting; no live requests or token measurements."""

import json

import pytest
from test_openai_responses import MockProvider, call, message, response
from test_openai_responses import no_provider_network as no_provider_network
from test_server_composition import data as data
from test_server_composition import no_development_database as no_development_database

from ai_operations_agent import evaluation
from ai_operations_agent.agent_schemas import StructuredAnswerDraft
from ai_operations_agent.agent_telemetry import RuntimeTrace
from ai_operations_agent.answer_composition import CompositionStrategy
from ai_operations_agent.evaluation_dataset import load_dataset


@pytest.mark.parametrize(("name", "legacy_count", "server_count"), [
    ("authorized-discovery", 2, 1), ("selected-all-signals", 2, 1),
    ("named-assessment", 3, 2), ("unsupported-capability", 1, 0),
])
@pytest.mark.parametrize("strategy", list(CompositionStrategy))
def test_paired_official_sdk_request_counts(data, name, legacy_count, server_count, strategy):
    pytest.importorskip("openai")  # Never report a count as verified when the SDK is absent.
    case = next(s for s in load_dataset().scenarios if s.id == name)
    demo = evaluation.build_adapter(case, data)
    drafts = []

    class CaptureFinal:
        def next_turn(self, *args):
            turn = demo.next_turn(*args)
            if isinstance(turn, StructuredAnswerDraft):
                drafts.append(turn)
            return turn

    # Capture the deterministic valid selection with the identical fixture/source IDs.
    assert evaluation.evaluate_scenario(case, data, adapter=CaptureFinal())["passed"]
    assert len(drafts) == 1
    script = []
    if name in {"authorized-discovery", "named-assessment"}:
        script.append(response(call()))
    if name in {"selected-all-signals", "named-assessment"}:
        queue = "all" if name == "selected-all-signals" else "healthy"
        script.append(response(call("get_queue_recommendations",
                                    json.dumps({"queue_id": data.queue_ids[queue]}), "assess")))
    if strategy is CompositionStrategy.MODEL_SELECTED:
        script.append(response(message(drafts[0])))
    provider = MockProvider(script)
    trace = RuntimeTrace()
    result = evaluation.evaluate_scenario(case, data, adapter=provider.adapter(trace=trace),
                                          trace=trace, composition_strategy=strategy)
    expected = legacy_count if strategy is CompositionStrategy.MODEL_SELECTED else server_count
    assert result["passed"]
    assert len(provider.requests) == result["provider_request_count"] == expected
    assert result["model_turn_count"] == expected
    assert result["final_model_selection"] == (
        "exercised" if strategy is CompositionStrategy.MODEL_SELECTED else "not_exercised"
    )
    assert all(value is None for value in result["usage"].values())
    assert not result["security_violations"]
    assert all(client.is_closed() for client in provider.clients)
    if expected == 0:
        assert provider.client_options == provider.clients == []
