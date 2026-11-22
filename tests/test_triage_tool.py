"""Trusted triage dispatch on real isolated SQLite/PostgreSQL, never a provider."""

import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from sqlite3 import OperationalError as SQLiteOperationalError

import pytest
from psycopg.errors import ReadOnlySqlTransaction
from pydantic import ValidationError
from sqlalchemy import event, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_queue_triage import (
    MISSING_ID,
    NOW,
    business_selects,
    statements,
)
from test_queue_triage import triage_data as triage_data

from ai_operations_agent import queue_triage, read_only, tools
from ai_operations_agent.agent_runtime import run_agent
from ai_operations_agent.agent_schemas import AgentRequest, ToolRequest
from ai_operations_agent.evaluation_fixtures import business_snapshot
from ai_operations_agent.fake_model import FakeModelAdapter
from ai_operations_agent.models import Queue
from ai_operations_agent.queue_triage import TriageError
from ai_operations_agent.read_only import ReadOnlyViolation
from ai_operations_agent.risk import RiskPolicy
from ai_operations_agent.tool_schemas import AccessContext, ExecutionLimits
from ai_operations_agent.tools import EvaluationContext, ToolError, execute_tool
from ai_operations_agent.triage_schemas import QueueTriageResult

CONTEXT = EvaluationContext(NOW, RiskPolicy())


def access_for(ids, **kwargs):
    return AccessContext(
        allowed_queue_ids=frozenset(ids),
        allowed_tool_names=frozenset({"get_queue_triage"}),
        **kwargs,
    )


def dispatch(engine, access, arguments=None, context=CONTEXT):
    return execute_tool(
        "get_queue_triage",
        {} if arguments is None else arguments,
        access,
        engine=engine,
        evaluation_context=context,
    )


@pytest.mark.parametrize("arguments", [{}, "{}", " { } "])
def test_exact_empty_arguments_read_only_and_detached_result(triage_data, arguments):
    data = triage_data
    high = data.queue(
        online=0, tickets=(dict(created_at=NOW - timedelta(minutes=31), assigned=False),)
    )
    low = data.queue()
    hidden = data.queue(
        online=0, tickets=tuple(dict(created_at=NOW - timedelta(days=1)) for _ in range(5))
    )
    before = business_snapshot(data.engine)
    with statements(data.engine) as observed:
        result = dispatch(data.engine, access_for({high, low, MISSING_ID}), arguments)
    assert result["scope"]["effective_queue_ids"] == sorted([high, low, MISSING_ID])
    assert [r["assessment"]["queue_id"] for r in result["rankings"]] == [high, low]
    assert result["unavailable_queue_ids"] == [MISSING_ID]
    assert result["available_queue_count"] == 2 and result["detail_target_queue_id"] == high
    assert hidden not in [r["assessment"]["queue_id"] for r in result["rankings"]]
    assert [r["position"] for r in result["rankings"]] == [1, 2]
    assert len(business_selects(observed)) == 2
    for sql in business_selects(observed):
        assert "WHERE" in sql and "IN" in sql
    assert business_snapshot(data.engine) == before
    QueueTriageResult.model_validate_json(json.dumps(result), strict=True)
    result["rankings"][0]["assessment"]["metrics"]["active_tickets"] = 999
    assert (
        dispatch(data.engine, access_for({high}))["rankings"][0]["assessment"]["metrics"][
            "active_tickets"
        ]
        == 1
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"queue_id": 1},
        {"queue_ids": []},
        {"filters": {}},
        {"allowed_queue_ids": [1]},
        {"allowed_tool_names": ["get_queue_triage"]},
        {"weights": {}},
        {"policy": {}},
        {"evaluated_at": "2026-10-31T16:00:00Z"},
        {"database_url": "PRIVATE"},
        {"engine": "PRIVATE"},
        {"model": "PRIVATE"},
        '{"queue_id":1,"queue_id":2}',
        '{"x":{},"x":{}}',
        "[]",
        "null",
        "1",
        "true",
        '"{}"',
        "[{}]",
        "{",
        [],
        1,
        True,
        b"{}",
        {"x": float("nan")},
        {"x": {1}},
        "{" + " " * 5000 + "}",
    ],
)
def test_rejects_arguments_before_any_connection(engine, monkeypatch, arguments):
    monkeypatch.setattr(engine, "connect", lambda: pytest.fail("connected for invalid arguments"))
    with pytest.raises(ToolError) as caught:
        dispatch(engine, access_for({1}), arguments)
    assert str(caught.value) in {"invalid tool arguments", "tool arguments exceed size limit"}
    assert caught.value.__context__ is None or caught.value.__suppress_context__


@pytest.mark.parametrize(
    "access,message",
    [
        (None, "tool access denied"),
        ({"allowed_queue_ids": [1]}, "tool access denied"),
        (AccessContext(allowed_queue_ids=frozenset({1})), "tool access denied"),
        (
            AccessContext(
                allowed_queue_ids=frozenset({1}), allowed_tool_names=tools.SUPPORTED_TOOLS
            ),
            "tool access denied",
        ),
        (access_for(set()), "tool access denied"),
        (access_for({1}, selected_queue_id=2), "queue unavailable"),
        (access_for(range(1, 34)), "triage scope exceeds limit"),
        (
            AccessContext.model_construct(
                allowed_queue_ids=frozenset({True}),
                allowed_tool_names=frozenset({"get_queue_triage"}),
            ),
            "tool access denied",
        ),
    ],
)
def test_exact_grants_and_scope_before_connection(engine, monkeypatch, access, message):
    monkeypatch.setattr(engine, "connect", lambda: pytest.fail("connected before authorization"))
    with pytest.raises(ToolError, match=f"^{message}$"):
        dispatch(engine, access)


def test_selected_queue_narrows_a_large_allowlist_and_missing_selection_fails(triage_data):
    data = triage_data
    qid = data.queue()
    ids = {qid, *range(MISSING_ID - 40, MISSING_ID)}
    result = dispatch(data.engine, access_for(ids, selected_queue_id=qid))
    assert result["scope"] == dict(
        mode="selected_queue", effective_queue_ids=[qid], selected_queue_id=qid
    )
    assert result["available_queue_count"] == 1
    with pytest.raises(ToolError, match="^queue unavailable$"):
        dispatch(data.engine, access_for({MISSING_ID}, selected_queue_id=MISSING_ID))


def test_max_scope_and_no_available_queues(triage_data):
    data = triage_data
    ids = [data.queue() for _ in range(32)]
    result = dispatch(data.engine, access_for(ids))
    assert len(result["rankings"]) == 32
    assert all(r["operational_tie"] for r in result["rankings"])
    empty = dispatch(data.engine, access_for({MISSING_ID}))
    assert empty["outcome"] == "no_available_queues" and empty["rankings"] == []


@pytest.mark.parametrize(
    "context",
    [
        None,
        {},
        EvaluationContext("2026-10-31T16:00:00Z", RiskPolicy()),
        EvaluationContext(datetime(2026, 10, 31), RiskPolicy()),
        EvaluationContext(
            datetime(2026, 10, 31, tzinfo=timezone(timedelta(hours=1))), RiskPolicy()
        ),
        EvaluationContext(NOW, None),
        EvaluationContext(NOW, {}),
    ],
)
def test_explicit_utc_evaluation_context_required_before_connection(engine, monkeypatch, context):
    monkeypatch.setattr(engine, "connect", lambda: pytest.fail("connected for invalid context"))
    with pytest.raises(ToolError, match="^invalid triage context$"):
        dispatch(engine, access_for({1}), context=context)


def test_no_default_database_clock_policy_or_credentials(triage_data, monkeypatch):
    data = triage_data
    qid = data.queue()

    def forbidden(*args, **kwargs):
        pytest.fail("read ambient database/time/policy")

    monkeypatch.setattr(read_only, "get_engine", forbidden)
    monkeypatch.setattr(tools, "get_evaluation_time", forbidden)
    monkeypatch.setattr(tools, "get_risk_policy", forbidden)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unread-marker")
    monkeypatch.setenv("DATABASE_URL", "invalid-unread-target")
    result = dispatch(data.engine, access_for({qid}))
    assert result["evaluated_at"] == NOW.isoformat().replace("+00:00", "Z")
    for engine_value in [None, object()]:
        with pytest.raises(ToolError, match="^invalid triage context$"):
            dispatch(engine_value, access_for({qid}))
    with Session(data.engine) as session:
        with pytest.raises(ToolError, match="^invalid triage context$"):
            dispatch(session, access_for({qid}))


@pytest.mark.parametrize(
    "failure,expected",
    [
        (TriageError("database unavailable"), "database unavailable"),
        (TriageError("queue unavailable"), "queue unavailable"),
        (TriageError("invalid triage data"), "invalid tool result"),
        (TriageError("PRIVATE SQL password queue name"), "invalid tool result"),
        (
            OperationalError("PRIVATE SELECT", {}, RuntimeError("PRIVATE database")),
            "database unavailable",
        ),
        (ValueError("PRIVATE values"), "invalid tool result"),
        (TypeError("PRIVATE config"), "invalid tool result"),
        (ReadOnlyViolation("PRIVATE writes"), "database unavailable"),
    ],
)
def test_expected_failures_are_fixed_sanitized_messages(engine, monkeypatch, failure, expected):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(queue_triage, "triage_queues", fail)
    with pytest.raises(ToolError, match=f"^{expected}$") as caught:
        dispatch(engine, access_for({1}))
    assert "PRIVATE" not in str(caught.value)
    assert caught.value.__context__ is None or caught.value.__suppress_context__


def test_atomic_exact_utf8_size_limit_and_no_transaction_during_validation(
    triage_data, monkeypatch
):
    data = triage_data
    qid = data.queue(online=0, tickets=(dict(created_at=NOW - timedelta(minutes=31)),))
    checked_out = set()

    def checkout(dbapi, record, proxy):
        checked_out.add(id(dbapi))

    def checkin(dbapi, record):
        checked_out.discard(id(dbapi))

    event.listen(data.engine, "checkout", checkout)
    event.listen(data.engine, "checkin", checkin)
    original = QueueTriageResult.model_validate_json

    def validated(*args, **kwargs):
        assert not checked_out, "business connection open during tool serialization"
        return original(*args, **kwargs)

    monkeypatch.setattr(QueueTriageResult, "model_validate_json", validated)
    try:
        access = access_for({qid})
        value = dispatch(data.engine, access)
        size = len(
            json.dumps(
                value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        )
        assert (
            dispatch(
                data.engine,
                access.model_copy(update={"limits": ExecutionLimits(max_result_bytes=size)}),
            )
            == value
        )
        with pytest.raises(ToolError, match="^tool result exceeds size limit$"):
            dispatch(
                data.engine,
                access.model_copy(update={"limits": ExecutionLimits(max_result_bytes=size - 1)}),
            )
        assert not checked_out
    finally:
        event.remove(data.engine, "checkout", checkout)
        event.remove(data.engine, "checkin", checkin)


@pytest.mark.parametrize("operation", ["orm", "native"])
def test_native_snapshot_read_only_and_cleanup_after_write_rejection(
    triage_data, monkeypatch, operation
):
    data = triage_data
    qid = data.queue()
    before = business_snapshot(data.engine)

    def attempted_write(db, scope, when, policy):
        assert db.execute(select(Queue.id).where(Queue.id == qid)).scalar_one() == qid
        if operation == "orm":
            db.execute(update(Queue).where(Queue.id == qid).values(sla_minutes=1))
        else:
            raw = db.connection().connection.driver_connection
            with closing(raw.cursor()) as cursor:
                if data.engine.dialect.name == "postgresql":
                    cursor.execute("SHOW transaction_isolation")
                    assert cursor.fetchone()[0] == "repeatable read"
                    expected = ReadOnlySqlTransaction
                else:
                    assert raw.in_transaction
                    assert raw.execute("PRAGMA query_only").fetchone()[0] == 1
                    expected = SQLiteOperationalError
                with pytest.raises(expected):
                    cursor.execute("UPDATE queues SET sla_minutes = 1")
            raise ReadOnlyViolation("synthetic failure after native rejection")

    monkeypatch.setattr(queue_triage, "collect_triage_metrics", attempted_write)
    with pytest.raises(ToolError, match="^database unavailable$"):
        dispatch(data.engine, access_for({qid}))
    assert business_snapshot(data.engine) == before
    with data.engine.connect() as connection:
        if data.engine.dialect.name == "sqlite":
            assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 0
            assert not connection.connection.driver_connection.in_transaction
        else:
            assert connection.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "off"


def test_database_statement_failure_rolls_back_and_releases_connection(triage_data):
    data = triage_data
    qid = data.queue()
    active = set()

    def checkout(dbapi, record, proxy):
        active.add(id(dbapi))

    def checkin(dbapi, record):
        active.discard(id(dbapi))

    def fail(conn, cursor, statement, params, context, executemany):
        if statement.startswith("SELECT "):
            raise OperationalError("PRIVATE SQL", {}, RuntimeError("PRIVATE error"))

    event.listen(data.engine, "checkout", checkout)
    event.listen(data.engine, "checkin", checkin)
    event.listen(data.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(ToolError, match="^database unavailable$"):
            dispatch(data.engine, access_for({qid}))
        assert not active
    finally:
        event.remove(data.engine, "checkout", checkout)
        event.remove(data.engine, "checkin", checkin)
        event.remove(data.engine, "before_cursor_execute", fail)


@pytest.mark.parametrize(
    "change", ["scope", "evaluation", "policy", "count", "position", "target", "outcome"]
)
def test_complete_result_revalidated_even_for_forged_model_instance(
    triage_data, monkeypatch, change
):
    data = triage_data
    qid = data.queue()
    canonical = data.run()
    if change == "scope":
        forged = canonical.model_copy(
            update={
                "scope": canonical.scope.model_copy(update={"effective_queue_ids": (MISSING_ID,)})
            }
        )
    elif change == "evaluation":
        shifted = NOW - timedelta(days=1)
        forged = data.run(evaluated_at=shifted)
    elif change == "policy":
        forged = data.run(policy=RiskPolicy(30, 2))
    elif change == "count":
        forged = canonical.model_copy(update={"available_queue_count": 2})
    elif change == "position":
        forged = canonical.model_copy(
            update={"rankings": (canonical.rankings[0].model_copy(update={"position": 2}),)}
        )
    elif change == "target":
        forged = canonical.model_copy(update={"detail_target_queue_id": qid})
    else:
        forged = canonical.model_copy(update={"outcome": "attention_required"})
    monkeypatch.setattr(queue_triage, "triage_queues", lambda *args, **kwargs: forged)
    with pytest.raises(ToolError, match="^invalid tool result$"):
        dispatch(data.engine, access_for({qid}))


def test_legacy_model_cannot_dispatch_triage_even_with_internal_grant(engine, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("triage reached dispatcher from legacy model")

    monkeypatch.setattr(tools, "execute_tool", forbidden)
    adapter = FakeModelAdapter([ToolRequest(call_id="call", name="get_queue_triage", arguments={})])
    response = run_agent(
        AgentRequest(question="triage", workflow="discovery"),
        AccessContext(allowed_queue_ids=frozenset({1}), allowed_tool_names=tools.INTERNAL_TOOLS),
        adapter,
        engine=engine,
    )
    assert response.status == "error" and response.error.code == "unsupported_tool"
    assert response.schema_version == "offline-agent-v1"
    assert not response.sources and not response.evidence
    assert tools.SUPPORTED_TOOLS == frozenset({"list_queues", "get_queue_recommendations"})
    from ai_operations_agent.openai_responses import _TOOL_ARGUMENTS

    assert set(_TOOL_ARGUMENTS) == {"list_queues", "get_queue_recommendations"}


def test_strict_grant_schema_does_not_accept_unknown_tools_or_types():
    for names in [{"arbitrary_sql"}, {True}, {1}]:
        with pytest.raises(ValidationError):
            AccessContext(allowed_tool_names=frozenset(names))
