"""Mocked official SDK transport; never a live model evaluation or a paid request."""

import builtins
import json
import os
import socket
import subprocess
import sys
from copy import deepcopy
from time import monotonic
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import event
from test_offline_agent_evaluations import (
    assert_failure,
    business_sql,
    discovery_answer,
    focus,
    ids,
)
from test_read_only_tools import access_for, business_rows
from test_read_only_tools import tool_data as tool_data

from ai_operations_agent import tools
from ai_operations_agent.agent_runtime import run_agent
from ai_operations_agent.agent_schemas import (
    AgentRequest,
    AgentResponse,
    ModelFailure,
    RuntimeLimits,
    StructuredAnswerDraft,
    ToolRequest,
)
from ai_operations_agent.main import app
from ai_operations_agent.openai_responses import OpenAIResponsesAdapter, ResponsesSettings
from ai_operations_agent.tool_schemas import ExecutionLimits


@pytest.fixture(autouse=True)
def no_provider_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("provider tests attempted live network access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    # This sentinel is used only with the mocked SDK transport, never a provider.
    monkeypatch.setenv("OPENAI_API_KEY", "mock-provider-sentinel")


def settings(**overrides):
    return ResponsesSettings(enabled=True, model_id="mock-model", **overrides)


def call(name="list_queues", arguments="{}", call_id="call_discovery", **overrides):
    return {"type": "function_call", "id": f"fc_{call_id}", "status": "completed",
            "call_id": call_id, "name": name, "arguments": arguments, **overrides}


def reasoning(suffix="1"):
    return {"type": "reasoning", "id": f"rs_{suffix}", "summary": [],
            "encrypted_content": f"opaque-mocked-reasoning-{suffix}"}


def message(draft=None, *, text=None, content=None, **overrides):
    if content is None:
        content = [{"type": "output_text", "annotations": [],
                    "text": text if text is not None else draft.model_dump_json()}]
    return {"type": "message", "id": "msg_final", "status": "completed", "role": "assistant",
            "content": content, **overrides}


def response(*items, **overrides):
    # Realistic /responses envelope, parsed by the official SDK through httpx.MockTransport.
    return {"id": "resp_mock", "object": "response", "created_at": 0,
            "status": "completed", "error": None, "incomplete_details": None,
            "model": "mock-model", "output": list(items), "parallel_tool_calls": False,
            "tools": [], "tool_choice": "auto", **overrides}


class MockProvider:
    def __init__(self, script):
        self.script = iter(deepcopy(script))
        self.requests = []
        self.client_options = []
        self.clients = []

    def handle(self, request):
        assert request.url == "https://api.openai.com/v1/responses"
        self.requests.append((json.loads(request.content), deepcopy(request.extensions)))
        outcome = next(self.script)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, tuple):
            status, body = outcome
        else:
            status, body = 200, outcome
        return httpx.Response(status, json=body)

    def factory(self, **kwargs):
        # Only these tests need the SDK installed. The core suite remains optional-SDK compatible.
        openai = pytest.importorskip("openai")
        self.client_options.append(kwargs)
        client = openai.OpenAI(**kwargs, http_client=httpx.Client(
            transport=httpx.MockTransport(self.handle), trust_env=False,
        ))
        self.clients.append(client)
        return client

    def adapter(self, config=None, **kwargs):
        return OpenAIResponsesAdapter(config or settings(), client_factory=self.factory, **kwargs)


def next_turn(adapter, conversation=None, permitted=("list_queues",), deadline=None):
    return adapter.next_turn(conversation or ({"role": "user", "request": {
        "question": "Discover queues", "workflow": "discovery", "queue_name": None,
    }},), permitted, StructuredAnswerDraft.model_json_schema(),
        deadline if deadline is not None else monotonic() + 10)


def run(tool_data, provider, *, workflow="queue_assessment", selected=True,
        request=None, access=None, adapter=None, **kwargs):
    engine, queues = tool_data
    access = access or access_for(queues, selected_queue_id=queues[0]["id"] if selected else None)
    result = run_agent(request or AgentRequest(question="Review this queue", workflow=workflow),
                       access, adapter or provider.adapter(), engine=engine,
                       new_id=kwargs.pop("new_id", ids()), **kwargs)
    assert AgentResponse.model_validate_json(result.model_dump_json(), strict=True) == result
    assert all(client.is_closed() for client in provider.clients)
    return result


def check_strict_schema(schema):
    # Deliberately limited to the documented provider vocabulary used by this milestone.
    assert set(schema) <= {"type", "properties", "required", "additionalProperties", "items",
                           "enum", "title", "exclusiveMinimum"}
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        for item in schema["properties"].values():
            check_strict_schema(item)
    if "items" in schema:
        check_strict_schema(schema["items"])


def test_discovery_only_strict_request_and_trusted_output(tool_data):
    provider = MockProvider([response(reasoning(), call()), response(message(discovery_answer()))])
    result = run(tool_data, provider, workflow="discovery", selected=False)
    assert result.status == "answered"
    assert result.sources[0].snapshot == tool_data[1]
    assert [item.value for item in result.evidence] == tool_data[1]
    assert len(provider.requests) == 2
    first, second = [body for body, _ in provider.requests]
    assert [t["name"] for t in first["tools"]] == ["list_queues"]
    assert second["tools"] == [] and second["tool_choice"] == "none"
    for body, _ in provider.requests:
        assert body["model"] == "mock-model"
        assert body["store"] is False and body["parallel_tool_calls"] is False
        assert body["max_output_tokens"] == 2048
        assert body["include"] == ["reasoning.encrypted_content"]
        assert "previous_response_id" not in body and "conversation" not in body
        for tool in body["tools"]:
            assert tool["type"] == "function" and tool["strict"] is True
            check_strict_schema(tool["parameters"])
        fmt = body["text"]["format"]
        assert fmt["type"] == "json_schema" and fmt["strict"] is True
        check_strict_schema(fmt["schema"])
        assert set(fmt["schema"]["properties"]) == set(discovery_answer().model_dump())
    assert provider.client_options[0]["max_retries"] == 0
    assert provider.client_options[0]["base_url"] == "https://api.openai.com/v1"
    history = second["input"]
    assert history[0] == first["input"][0]
    assert history[1:3] == [reasoning(), call()]
    output = history[3]
    assert output["type"] == "function_call_output" and output["call_id"] == "call_discovery"
    trusted = json.loads(output["output"])
    assert trusted["source"] == result.sources[0].model_dump(mode="json")
    assert trusted["references"] == [item.reference.model_dump() for item in result.evidence]


def test_direct_assessment_preserves_canonical_evidence_and_no_writes(tool_data):
    engine, queues = tool_data
    qid = queues[0]["id"]
    provider = MockProvider([
        response(call("get_queue_recommendations", json.dumps({"queue_id": qid}), "call_assess")),
        response(message(focus())),
    ])
    active = []
    original_handle = provider.handle

    def handle(request):
        assert active == [], "provider called with a database connection still checked out"
        return original_handle(request)

    def checkout(*args):
        active.append(True)

    def checkin(*args):
        active.pop()

    provider.handle = handle
    before = business_rows(engine)
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        with business_sql(engine) as sql, patch("sqlalchemy.orm.Session.flush", side_effect=AssertionError), \
                patch("sqlalchemy.orm.Session.commit", side_effect=AssertionError):
            result = run(tool_data, provider)
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
    assert result.status == "answered"
    assert business_rows(engine) == before
    assert len(sql) == 2 and all(s.lstrip().startswith("SELECT ") for s in sql)
    snapshot = result.sources[0].snapshot
    assert [e.value for e in result.evidence[1:]] == snapshot["recommendations"]
    for recommendation, block in zip(snapshot["recommendations"], result.explanation[1:], strict=True):
        assert recommendation["suggested_action"] in block.text
        assert recommendation["explanation"] in block.text
    first = provider.requests[0][0]
    assert [t["name"] for t in first["tools"]] == ["get_queue_recommendations"]
    check_strict_schema(first["tools"][0]["parameters"])


def test_named_assessment_full_stateless_replay_and_correlation(tool_data):
    engine, queues = tool_data
    assess = call("get_queue_recommendations", json.dumps({"queue_id": queues[0]["id"]}), "assess")
    provider = MockProvider([response(reasoning(), call()), response(reasoning("2"), assess),
                             response(message(focus(source="id_2")))])
    with business_sql(engine) as sql:
        result = run(tool_data, provider, selected=False, request=AgentRequest(
            question="Review named queue", workflow="queue_assessment", queue_name=queues[0]["name"],
        ))
    assert result.status == "answered" and len(sql) == 3
    first, second, third = [body for body, _ in provider.requests]
    assert [t["name"] for t in second["tools"]] == ["get_queue_recommendations"]
    assert third["tools"] == []
    assert third["input"][:4] == second["input"]
    assert third["input"][4:6] == [reasoning("2"), assess]
    outputs = [item for item in third["input"] if item.get("type") == "function_call_output"]
    assert [item["call_id"] for item in outputs] == ["call_discovery", "assess"]
    assert [json.loads(o["output"])["source"] for o in outputs] == [
        source.model_dump(mode="json") for source in result.sources
    ]
    assert first["input"][0] == third["input"][0]


@pytest.mark.parametrize("raw", [
    '{"queue_id":1,"queue_id":2}', '{"queue_id":1,"queue_id":1}',
    '{"queue_id":true}', '{"queue_id":false}', '{"queue_id":"1"}', '{"queue_id":1.0}',
    '{"queue_id":1.5}', '{"queue_id":0}', '{"queue_id":-1}', '{"queue_id":null}',
    '{"queue_id":[]}', '{"queue_id":{}}', '{"queue_id":NaN}', '{"queue_id":Infinity}',
    '{}', '[]', 'null', '{', '{"queue_id":1, "extra":0}',
    '{"queue_id":1,"config":{"x":1,"x":2}}', ' ' * 4097 + '{"queue_id":1}',
    '{"queue_id":1,"extra":"' + "é" * 2500 + '"}', {"queue_id": 1},
])
def test_raw_argument_boundary_fails_before_dispatch(tool_data, raw):
    provider = MockProvider([response(call("get_queue_recommendations", raw))])
    with patch.object(tools, "execute_tool", side_effect=AssertionError("dispatch occurred")):
        result = run(tool_data, provider)
    assert_failure(result, "model_unavailable")
    assert len(provider.requests) == 1 and result.sources == ()


@pytest.mark.parametrize("name", ["list_queues", "shell", "sql", "web_search", "create_ticket"])
def test_disallowed_or_unknown_tool(tool_data, name):
    provider = MockProvider([response(call(name))])
    with patch.object(tools, "execute_tool", side_effect=AssertionError("dispatch occurred")):
        result = run(tool_data, provider)
    assert_failure(result, "model_unavailable")


@pytest.mark.parametrize("items", [
    [call(), call(call_id="second")],
    [call(), call()],
    [call(), message(discovery_answer())],
    [call(), message(content=[{"type": "refusal", "refusal": "PRIVATE"}])],
    [message(discovery_answer()), message(discovery_answer())],
    [{"type": "function_call_output", "call_id": "call_discovery", "output": "invented facts"}],
    [{"type": "web_search_call", "id": "ws_1", "status": "completed"}],
    [reasoning()],
    [],
])
def test_ambiguous_or_unknown_outcomes(tool_data, items):
    provider = MockProvider([response(*items)])
    result = run(tool_data, provider, workflow="discovery", selected=False)
    assert_failure(result, "model_unavailable")
    assert result.sources == ()


@pytest.mark.parametrize("call_id", ["", "has spaces", "a" * 65, "bad/slash", None, 1])
def test_invalid_call_id(call_id):
    provider = MockProvider([response(call(call_id=call_id))])
    assert next_turn(provider.adapter()) == ModelFailure(reason="unavailable")


def test_sdk_direct_caller_is_supported():
    provider = MockProvider([response(call(caller={"type": "direct"}))])
    with provider.adapter().open_run() as adapter:
        assert isinstance(next_turn(adapter), ToolRequest)


@pytest.mark.parametrize("extra", [
    {"caller": {"type": "program", "program_id": "unoffered"}},
    {"namespace": "unoffered"}, {"output": "model-authored result"},
])
def test_unoffered_call_extensions_are_rejected(extra):
    provider = MockProvider([response(call(**extra))])
    assert next_turn(provider.adapter()) == ModelFailure(reason="unavailable")


def test_duplicate_call_id_across_turns(tool_data):
    queues = tool_data[1]
    provider = MockProvider([response(call()), response(call(
        "get_queue_recommendations", json.dumps({"queue_id": queues[0]["id"]}), "call_discovery",
    ))])
    result = run(tool_data, provider, selected=False, request=AgentRequest(
        question="Review", workflow="queue_assessment", queue_name=queues[0]["name"],
    ))
    assert_failure(result, "model_unavailable")
    assert len(result.sources) == 1


@pytest.mark.parametrize("changes", [
    {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
    {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}},
    {"status": "failed", "error": {"code": "server_error", "message": "PRIVATE"}},
    {"status": "in_progress"}, {"status": "queued"}, {"status": "cancelled"},
    {"incomplete_details": {"reason": "max_output_tokens"}},
    {"error": {"code": "server_error", "message": "PRIVATE"}},
])
def test_incomplete_failed_truncated_responses(changes):
    provider = MockProvider([response(message(discovery_answer()), **changes)])
    assert next_turn(provider.adapter()) == ModelFailure(reason="unavailable")
    assert len(provider.requests) == 1


@pytest.mark.parametrize("item", [
    message(discovery_answer(), status="incomplete"),
    call(status="in_progress"),
    {**reasoning(), "encrypted_content": None},
    message(content=[]),
    message(content=[{"type": "output_text", "text": "{}", "annotations": []},
                     {"type": "refusal", "refusal": "PRIVATE"}]),
    message(content=[{"type": "output_text", "text": "{}", "annotations": []}] * 2),
])
def test_incomplete_or_ambiguous_items(item):
    provider = MockProvider([response(item)])
    assert next_turn(provider.adapter()) == ModelFailure(reason="unavailable")


def test_refusal_is_sanitized(tool_data):
    provider = MockProvider([response(message(content=[{"type": "refusal", "refusal": "PRIVATE"}]))])
    result = run(tool_data, provider)
    assert result.status == "unsupported" and result.uncertainty == ("model_refusal",)
    assert "PRIVATE" not in result.model_dump_json()
    assert len(provider.requests) == 1


@pytest.mark.parametrize("text", [
    '{', '{"type":"answer"', '{}', '[]', 'null', 'a natural language answer',
    discovery_answer().model_dump_json().replace('"kind":"discovery"', '"kind":"discovery","kind":"queue_focus"'),
    discovery_answer().model_dump_json().replace('"inability":null', '"inability":null,"counts":1'),
    discovery_answer().model_dump_json().replace('"inability":null', '"inability":null,"severity":"high"'),
    discovery_answer().model_dump_json().replace('"inability":null', '"inability":null,"prose":"hello"'),
    discovery_answer().model_dump_json().replace('"inability":null', '"inability":null,"evidence":{}'),
    discovery_answer().model_dump_json().replace('"references":[', '"references":[').replace('"source_id":"id_1"', '"source_id":1'),
    discovery_answer().model_dump_json().replace('"inability":null', '"inability":0'),
    ' ' * 8193 + discovery_answer().model_dump_json(),
])
def test_invalid_structured_content(text):
    provider = MockProvider([response(message(text=text))])
    assert next_turn(provider.adapter(), permitted=()) == ModelFailure(reason="unavailable")


@pytest.mark.parametrize("bad_draft", [
    focus(codes=()), focus(codes=("INVENTED",)),
    focus().model_copy(update={"references": focus().references[:-1]}),
    focus().model_copy(update={"recommendation_codes": tuple(reversed(focus().recommendation_codes))}),
    focus(source="other_run_source"),
])
def test_schema_valid_drafts_still_require_grounding(tool_data, bad_draft):
    qid = tool_data[1][0]["id"]
    provider = MockProvider([response(call("get_queue_recommendations", json.dumps({"queue_id": qid}))),
                             response(message(bad_draft))])
    result = run(tool_data, provider)
    assert_failure(result, "invalid_answer")
    assert len(result.sources) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429, 500, 503])
def test_sdk_errors_sanitized_without_retries(tool_data, status, caplog):
    provider = MockProvider([(status, {"error": {"message": "PRIVATE authorization mock-provider-sentinel",
                                                "type": "insufficient_quota", "code": "quota"}})])
    result = run(tool_data, provider)
    assert_failure(result, "model_unavailable")
    assert len(provider.requests) == 1
    assert "PRIVATE" not in result.model_dump_json() + caplog.text
    assert "mock-provider-sentinel" not in result.model_dump_json() + caplog.text


@pytest.mark.parametrize("error", [httpx.ReadTimeout("PRIVATE"), httpx.ConnectTimeout("PRIVATE"),
                                   httpx.ConnectError("PRIVATE"), httpx.RemoteProtocolError("PRIVATE")])
def test_sdk_timeout_and_network_failure(tool_data, error):
    provider = MockProvider([error])
    result = run(tool_data, provider)
    assert_failure(result, "deadline_exceeded" if isinstance(error, httpx.TimeoutException)
                   else "model_unavailable")
    assert len(provider.requests) == 1 and "PRIVATE" not in result.model_dump_json()


@pytest.mark.parametrize("key", [None, "", "   "])
def test_missing_credentials_never_construct_client(monkeypatch, key):
    if key is None:
        monkeypatch.delenv("OPENAI_API_KEY")
    else:
        monkeypatch.setenv("OPENAI_API_KEY", key)
    def factory(**kwargs):
        pytest.fail("client constructed without credentials")

    assert next_turn(OpenAIResponsesAdapter(settings(), client_factory=factory)) == ModelFailure(
        reason="unavailable")


def test_disabled_default_and_construction_are_lazy(monkeypatch):
    def factory(**kwargs):
        pytest.fail("disabled adapter constructed a client")

    original_get = os.environ.get

    def guarded_get(key, *args):
        if key.startswith("OPENAI_"):
            pytest.fail("provider environment read while disabled")
        return original_get(key, *args)

    monkeypatch.setattr(os.environ, "get", guarded_get)
    adapter = OpenAIResponsesAdapter(client_factory=factory)
    assert next_turn(adapter) == ModelFailure(reason="unavailable")
    with adapter.open_run() as run_scope:
        assert run_scope._client is None


def test_optional_sdk_absent_is_sanitized(monkeypatch):
    original = builtins.__import__

    def without_openai(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("PRIVATE optional SDK unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_openai)
    assert next_turn(OpenAIResponsesAdapter(settings())) == ModelFailure(reason="unavailable")


def test_import_has_no_sdk_or_secret_access():
    subprocess.run([sys.executable, "-c", """
import builtins
import os
import socket
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'openai':
        raise AssertionError('SDK imported')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
original_get = os.environ.get
def guarded_get(key, *args):
    if key.startswith('OPENAI_'):
        raise AssertionError('provider environment read during import')
    return original_get(key, *args)
os.environ.get = guarded_get
socket.create_connection = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('network'))
import ai_operations_agent.openai_responses
"""], check=True, capture_output=True, text=True)


@pytest.mark.parametrize("overrides", [
    {"enabled": True}, {"enabled": 1}, {"model_id": ""}, {"model_id": "https://provider.invalid"},
    {"max_requests": 0}, {"max_requests": 4}, {"max_requests": True},
    {"timeout_seconds": 0}, {"timeout_seconds": 31}, {"timeout_seconds": float("inf")},
    {"max_output_tokens": True}, {"max_output_tokens": 0}, {"max_output_tokens": 8193},
    {"max_argument_bytes": 4097}, {"max_payload_bytes": 196609}, {"base_url": "https://invalid"},
    {"api_key": "never accepted in settings"},
])
def test_invalid_provider_configuration(overrides):
    with pytest.raises(ValidationError):
        ResponsesSettings(**overrides)


def test_deadline_exhausted_before_client_or_request():
    provider = MockProvider([])
    for deadline in (4.0, float("nan"), float("inf")):
        assert next_turn(provider.adapter(clock=lambda: 4.0), deadline=deadline) == ModelFailure(
            reason="timeout")
    assert provider.clients == [] and provider.requests == []


def test_client_preparation_consumes_deadline():
    now = [1.0]
    provider = MockProvider([])

    def factory(**kwargs):
        client = provider.factory(**kwargs)
        now[0] = 5.0
        return client

    adapter = OpenAIResponsesAdapter(settings(), client_factory=factory, clock=lambda: now[0])
    assert next_turn(adapter, deadline=5.0) == ModelFailure(reason="timeout")
    assert provider.requests == [] and provider.clients[0].is_closed()


@pytest.mark.parametrize(("deadline", "expected"), [(9.0, 0.25), (100.0, 2.0)])
def test_remaining_deadline_bounds_every_http_phase(deadline, expected):
    provider = MockProvider([response(call())])
    adapter = provider.adapter(settings(timeout_seconds=2.0, max_output_tokens=512), clock=lambda: 8.75)
    assert isinstance(next_turn(adapter, deadline=deadline), ToolRequest)
    assert set(provider.requests[0][1]["timeout"].values()) == {expected}
    assert provider.requests[0][0]["max_output_tokens"] == 512
    adapter.close()


def test_late_provider_response_is_rejected(tool_data):
    now = [1.0]
    provider = MockProvider([response(call())])
    handle = provider.handle

    def late(request):
        reply = handle(request)
        now[0] = 50.0
        return reply

    provider.handle = late
    result = run(tool_data, provider, workflow="discovery", adapter=provider.adapter(clock=lambda: now[0]),
                 clock=lambda: now[0], limits=RuntimeLimits(timeout_seconds=10.0))
    assert_failure(result, "deadline_exceeded")
    assert not result.sources


def test_provider_request_budget_stops_without_retry(tool_data):
    provider = MockProvider([response(call())])
    result = run(tool_data, provider, workflow="discovery", selected=False,
                 adapter=provider.adapter(settings(max_requests=1)))
    assert_failure(result, "model_unavailable")
    assert len(provider.requests) == 1 and len(result.sources) == 1


def test_lower_dispatcher_argument_budget_still_enforced(tool_data):
    qid = tool_data[1][0]["id"]
    provider = MockProvider([response(call("get_queue_recommendations", json.dumps({"queue_id": qid})))])
    access = access_for(tool_data[1], selected_queue_id=qid).model_copy(update={
        "limits": ExecutionLimits(max_argument_bytes=1),
    })
    result = run(tool_data, provider, access=access)
    assert_failure(result, "invalid_arguments")
    assert not result.sources


def test_oversized_provider_request_never_calls_transport():
    provider = MockProvider([])
    conversation = ({"role": "user", "request": AgentRequest(
        question="é" * 2500, workflow="discovery",
    ).model_dump()},)
    result = next_turn(provider.adapter(settings(max_payload_bytes=4096)), conversation=conversation)
    assert result == ModelFailure(reason="unavailable") and provider.requests == []
    assert all(client.is_closed() for client in provider.clients)


def test_oversized_response_never_enters_evidence(tool_data):
    provider = MockProvider([response({**reasoning(), "encrypted_content": "x" * 5000}, call())])
    result = run(tool_data, provider, workflow="discovery",
                 adapter=provider.adapter(settings(max_payload_bytes=4096)))
    assert_failure(result, "model_unavailable")
    assert len(provider.requests) == 1 and result.sources == ()


def test_replay_request_budget_includes_native_provider_items(tool_data):
    provider = MockProvider([response({**reasoning(), "encrypted_content": "x" * 3000}, call())])
    result = run(tool_data, provider, workflow="discovery",
                 adapter=provider.adapter(settings(max_payload_bytes=4096)))
    assert_failure(result, "model_unavailable")
    assert len(provider.requests) == 1 and len(result.sources) == 1


def test_malformed_sdk_values_do_not_emit_input_warnings(recwarn):
    provider = MockProvider([response(call(arguments={"secret": "PRIVATE"}))])
    assert next_turn(provider.adapter()) == ModelFailure(reason="unavailable")
    assert not recwarn.list


@pytest.mark.parametrize("permitted", [("sql",), ("list_queues", "list_queues")])
def test_invalid_permitted_tools_never_construct_client(permitted):
    provider = MockProvider([])
    assert next_turn(provider.adapter(), permitted=permitted) == ModelFailure(reason="unavailable")
    assert provider.clients == []


@pytest.mark.parametrize("tamper", ["call_id", "outcome", "prefix"])
def test_continuation_corruption_never_calls_provider_again(tamper):
    provider = MockProvider([response(call())])
    conversation = ({"role": "user", "request": AgentRequest(
        question="Discover queues", workflow="discovery",
    ).model_dump()},)
    adapter = provider.adapter()
    turn = next_turn(adapter, conversation=conversation)
    assert isinstance(turn, ToolRequest)
    continued = deepcopy(conversation) + (
        {"role": "assistant", "outcome": turn.model_dump()},
        {"role": "tool", "call_id": turn.call_id,
         "source": {"tool_name": turn.name, "snapshot": []}, "references": []},
    )
    if tamper == "call_id":
        continued[-1]["call_id"] = "wrong_call"
    elif tamper == "outcome":
        continued[-2]["outcome"]["arguments"] = {"extra": True}
    else:
        continued[0]["request"]["question"] = "another run"
    assert next_turn(adapter, conversation=continued) == ModelFailure(reason="unavailable")
    assert len(provider.requests) == 1


def test_cross_run_state_isolation_with_reused_adapter(tool_data):
    answer = discovery_answer()
    second_answer = answer.model_copy(update={"references": tuple(
        ref.model_copy(update={"source_id": "id_3"}) for ref in answer.references
    )})
    provider = MockProvider([response(reasoning(), call()), response(message(discovery_answer())),
                             response(call()), response(message(second_answer))])
    adapter = provider.adapter()
    new_id = ids()
    first = run(tool_data, provider, workflow="discovery", selected=False, adapter=adapter, new_id=new_id)
    second = run(tool_data, provider, workflow="discovery", selected=False, adapter=adapter, new_id=new_id)
    assert first.status == second.status == "answered"
    assert first.run_id != second.run_id and first.sources[0].source_id != second.sources[0].source_id
    assert len(provider.clients) == 2
    assert len(provider.requests[2][0]["input"]) == 1
    assert "opaque-mocked" not in json.dumps(provider.requests[2][0])
    assert first.sources[0].source_id not in json.dumps(provider.requests[3][0])
    assert adapter._history == [] and adapter._conversation == () and adapter._client is None


def test_overlapping_run_scopes_have_independent_histories():
    provider = MockProvider([response(reasoning("first"), call(call_id="same_call")),
                             response(reasoning("second"), call(call_id="same_call"))])
    root = provider.adapter()
    with root.open_run() as first, root.open_run() as second:
        assert isinstance(next_turn(first), ToolRequest)
        assert isinstance(next_turn(second), ToolRequest)
        assert first._history[1] == reasoning("first")
        assert second._history[1] == reasoning("second")
        assert len(provider.requests[1][0]["input"]) == 1
    assert first._history == second._history == []


def test_direct_scope_rejects_restarted_conversation():
    provider = MockProvider([response(call())])
    adapter = provider.adapter()
    assert isinstance(next_turn(adapter), ToolRequest)
    assert next_turn(adapter) == ModelFailure(reason="unavailable")
    assert len(provider.requests) == 1 and adapter._history == []


def test_scope_cleanup_on_tool_failure(tool_data, monkeypatch):
    provider = MockProvider([response(call())])
    monkeypatch.setattr(tools, "execute_tool", lambda *a, **kw: (_ for _ in ()).throw(
        tools.ToolError("database unavailable")))
    result = run(tool_data, provider, workflow="discovery")
    assert_failure(result, "database_unavailable")


def test_no_new_http_endpoints_or_startup_provider_access(client, monkeypatch):
    monkeypatch.setattr(OpenAIResponsesAdapter, "__init__", lambda *a, **kw: pytest.fail("provider constructed"))
    assert not any("agent" in route.path and route.path != "/api/v1/agents" for route in app.routes)
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    assert client.get("/api/v1/queues").status_code == 200


def test_provider_is_not_used_for_missing_queue_name(tool_data):
    provider = MockProvider([])
    result = run(tool_data, provider, selected=False)
    assert result.status == "needs_clarification" and provider.clients == []


def test_unsupported_workflow_exposes_no_tools(tool_data):
    draft = StructuredAnswerDraft(kind="inability", inability="unsupported_capability")
    provider = MockProvider([response(message(draft))])
    result = run(tool_data, provider, workflow="unsupported")
    assert result.status == "unsupported" and provider.requests[0][0]["tools"] == []
