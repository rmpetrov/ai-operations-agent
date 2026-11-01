import json
import socket
from contextlib import closing
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from sqlite3 import OperationalError as SQLiteOperationalError
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg.errors import ReadOnlySqlTransaction
from psycopg.pq import TransactionStatus
from pydantic import ValidationError
from sqlalchemy import create_engine, delete, event, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ai_operations_agent import assessment, queue_services, read_only, tools
from ai_operations_agent.clock import get_evaluation_time
from ai_operations_agent.config import get_risk_policy
from ai_operations_agent.db import get_session
from ai_operations_agent.main import app
from ai_operations_agent.models import Agent, Queue, Ticket
from ai_operations_agent.read_only import ReadOnlyViolation, read_only_session
from ai_operations_agent.risk import RiskPolicy
from ai_operations_agent.schemas import QueueRecommendations
from ai_operations_agent.tool_schemas import (
    AccessContext,
    ExecutionLimits,
    QueueIdentity,
    QueueRecommendationsArguments,
    QueueRecommendationsResult,
)
from ai_operations_agent.tools import ToolError, execute_tool

NOW = datetime(2026, 10, 31, 16, tzinfo=timezone.utc)
TABLES = (Queue.__table__, Agent.__table__, Ticket.__table__)


def business_rows(engine):
    with engine.connect() as conn:
        return [conn.execute(select(table).order_by(table.c.id)).all() for table in TABLES]


@pytest.fixture
def tool_data(engine, monkeypatch):
    """Commit only dedicated test fixtures so separate read-only transactions can see them.

    Never change the transaction mode of existing write/savepoint fixtures. Delete precisely
    these fixtures on teardown and verify all pre-existing business rows are unchanged.
    """
    before = business_rows(engine)
    prefix = uuid4().hex[:16]
    queue_ids = []
    try:
        with Session(engine) as seed:
            first = Queue(name=f"{prefix} Billing", sla_minutes=30)
            second = Queue(name=f"{prefix} Technical", sla_minutes=45)
            seed.add_all([first, second])
            seed.flush()
            queue_ids = [first.id, second.id]
            seed.add_all([
                Agent(name="PRIVATE OPERATOR OFFLINE", queue_id=first.id, status="offline"),
                Agent(name="PRIVATE OPERATOR ONLINE", queue_id=second.id, status="online"),
                Ticket(external_id=f"{prefix}-1", subject="PRIVATE OVERDUE SUBJECT",
                       queue_id=first.id, priority="high", status="open",
                       created_at=NOW - timedelta(minutes=31)),
                Ticket(external_id=f"{prefix}-2", subject="PRIVATE UPCOMING SUBJECT",
                       queue_id=first.id, priority="normal", status="in_progress",
                       created_at=NOW - timedelta(minutes=25)),
            ])
            seed.commit()
            identities = [{"id": first.id, "name": first.name},
                          {"id": second.id, "name": second.name}]
        monkeypatch.setattr(tools, "get_evaluation_time", lambda: NOW)
        monkeypatch.setattr(tools, "get_risk_policy", lambda: RiskPolicy())
        yield engine, identities
    finally:
        if queue_ids:
            with engine.begin() as cleanup:
                cleanup.execute(delete(Ticket).where(Ticket.queue_id.in_(queue_ids)))
                cleanup.execute(delete(Agent).where(Agent.queue_id.in_(queue_ids)))
                cleanup.execute(delete(Queue).where(Queue.id.in_(queue_ids)))
        assert business_rows(engine) == before


def access_for(identities, **kwargs):
    return AccessContext(
        allowed_queue_ids=frozenset(item["id"] for item in identities),
        allowed_tool_names=tools.SUPPORTED_TOOLS, **kwargs,
    )


def test_exact_scoped_discovery_and_selected_queue(tool_data):
    engine, identities = tool_data
    assert execute_tool("list_queues", {}, access_for(identities), engine=engine) == identities
    scope = access_for(identities[1:])
    assert execute_tool("list_queues", "{}", scope, engine=engine) == identities[1:]
    selected = access_for(identities, selected_queue_id=identities[0]["id"])
    assert execute_tool("list_queues", {}, selected, engine=engine) == identities[:1]
    assert execute_tool("list_queues", {}, access_for([{"id": 999_999}]), engine=engine) == []


def test_filtering_happens_in_sql_and_lists_only_queue_data(tool_data):
    engine, identities = tool_data
    statements = []

    def record(conn, cursor, sql, params, context, executemany):
        if "queues" in sql.lower():
            statements.append((sql, params))

    event.listen(engine, "before_cursor_execute", record)
    try:
        result = execute_tool("list_queues", {}, access_for(identities[1:]), engine=engine)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert result == identities[1:]
    assert len(statements) == 1
    sql, params = statements[0]
    assert "WHERE queues.id IN" in sql
    assert "ORDER BY queues.id" in sql
    assert "tickets" not in sql and "agents" not in sql
    assert identities[1]["id"] in (params.values() if isinstance(params, dict) else params)


@pytest.mark.parametrize("context", [
    None, {}, AccessContext(),
    AccessContext(allowed_tool_names=tools.SUPPORTED_TOOLS),
    AccessContext(allowed_queue_ids=frozenset({1})),
])
def test_deny_by_default_without_database_access(monkeypatch, context):
    def unexpected(*args, **kwargs):
        pytest.fail("denied request reached database session factory")

    monkeypatch.setattr(tools, "read_only_session", unexpected)
    with pytest.raises(ToolError, match="^tool access denied$"):
        execute_tool("list_queues", {}, context)


@pytest.mark.parametrize("name", ["sql", "get_ticket", "shell", "create_ticket", "", None, [], 42])
def test_unsupported_tools_rejected_before_database_access(monkeypatch, name):
    with patch.object(tools, "read_only_session", side_effect=AssertionError("database access")):
        with pytest.raises(ToolError, match="^unsupported tool$"):
            execute_tool(name, {}, AccessContext())


@pytest.mark.parametrize("queue_id", [True, False, "1", 1.0, 1.5, 0, -1, None, [], {}])
def test_strict_queue_arguments_before_database_access(queue_id):
    access = AccessContext(allowed_queue_ids=frozenset({1}),
                           allowed_tool_names=tools.SUPPORTED_TOOLS)
    with patch.object(tools, "read_only_session", side_effect=AssertionError("database access")):
        for arguments in ({"queue_id": queue_id}, json.dumps({"queue_id": queue_id})):
            with pytest.raises(ToolError, match="^invalid tool arguments$"):
                execute_tool("get_queue_recommendations", arguments, access)


@pytest.mark.parametrize(("name", "arguments"), [
    ("list_queues", {"allowed_queue_ids": [1]}),
    ("list_queues", {"question": "show all queues"}),
    ("list_queues", {"queue_id": 1}),
    ("get_queue_recommendations", {}),
    ("get_queue_recommendations", {"queue_id": 1, "allowed_tool_names": ["sql"]}),
    ("get_queue_recommendations", {"queue_id": 1, "selected_queue_id": 2}),
    ("get_queue_recommendations", '{"queue_id":1,"queue_id":2}'),
    ("list_queues", None), ("list_queues", []), ("list_queues", "[]"),
    ("list_queues", "null"), ("list_queues", "{"),
    ("list_queues", '{"padding": "' + "x" * 5000 + '"}'),
    ("list_queues", " " * 5000 + "{}"),
    ("list_queues", {"x": object()}),
])
def test_malformed_and_oversized_arguments(name, arguments):
    with patch.object(tools, "read_only_session", side_effect=AssertionError("database access")):
        with pytest.raises(ToolError):
            execute_tool(name, arguments, access_for([{"id": 1}]))


def test_byte_limits_and_extra_permission_fields():
    access = access_for([{"id": 1}], limits=ExecutionLimits(max_argument_bytes=20))
    with patch.object(tools, "read_only_session", side_effect=AssertionError("database access")):
        with pytest.raises(ToolError, match="exceed size"):
            execute_tool("list_queues", '{"x":"' + "é" * 8 + '"}', access)
        with pytest.raises(ToolError, match="invalid tool arguments"):
            execute_tool("get_queue_recommendations", {"queue_id": 1, "scope": []},
                         access_for([{"id": 1}]))


def test_context_is_immutable_and_strict():
    access = access_for([{"id": 1}])
    with pytest.raises(ValidationError):
        access.selected_queue_id = 2
    with pytest.raises(AttributeError):
        access.allowed_queue_ids.add(2)
    with pytest.raises(ValidationError):
        access.limits.max_argument_bytes = 10000
    for bad in (True, "1", 1.0, 0, -1):
        with pytest.raises(ValidationError):
            AccessContext(allowed_queue_ids=frozenset({bad}))
        with pytest.raises(ValidationError):
            AccessContext(selected_queue_id=bad)
    with pytest.raises(ValidationError):
        AccessContext(allowed_tool_names=frozenset({"sql"}))
    with pytest.raises(ValidationError):
        ExecutionLimits(max_result_bytes=0)
    with pytest.raises(ValidationError):
        QueueIdentity(id=True, name="bad")
    with pytest.raises(ValidationError):
        QueueRecommendationsArguments(queue_id=1, permissions={})


def test_forbidden_queue_and_selection_rejected_without_queries(tool_data):
    engine, identities = tool_data
    first, second = (item["id"] for item in identities)
    cases = [
        ("get_queue_recommendations", {"queue_id": second}, access_for(identities[:1])),
        ("get_queue_recommendations", {"queue_id": 999_999}, access_for(identities)),
        ("get_queue_recommendations", {"queue_id": second},
         access_for(identities, selected_queue_id=first)),
        ("list_queues", {}, access_for(identities[:1], selected_queue_id=second)),
    ]
    with patch.object(tools, "read_only_session", side_effect=AssertionError("database access")):
        for name, args, access in cases:
            with pytest.raises(ToolError, match="^queue unavailable$"):
                execute_tool(name, args, access, engine=engine)
    forbidden_tool = AccessContext(allowed_queue_ids=frozenset({first}),
                                   allowed_tool_names=frozenset({"list_queues"}))
    with patch.object(tools, "read_only_session", side_effect=AssertionError("database access")):
        with pytest.raises(ToolError, match="^tool access denied$"):
            execute_tool("get_queue_recommendations", {"queue_id": first}, forbidden_tool)


@pytest.mark.parametrize("name", ["list_queues", "get_queue_recommendations"])
def test_nonexistent_selected_and_forbidden_selected_are_indistinguishable(tool_data, name):
    engine, identities = tool_data
    missing = 999_999
    args = {} if name == "list_queues" else {"queue_id": missing}
    errors = []
    for scope in (identities, [{"id": missing}]):
        with pytest.raises(ToolError) as exc:
            execute_tool(name, args, access_for(scope, selected_queue_id=missing), engine=engine)
        errors.append(str(exc.value))
    assert errors == ["queue unavailable", "queue unavailable"]


def test_tool_matches_canonical_api_and_preserves_queue_contract(tool_data, monkeypatch):
    engine, identities = tool_data
    queue_id = identities[0]["id"]
    with Session(engine) as api_db:
        monkeypatch.setitem(app.dependency_overrides, get_session, lambda: api_db)
        monkeypatch.setitem(app.dependency_overrides, get_evaluation_time, lambda: NOW)
        monkeypatch.setitem(app.dependency_overrides, get_risk_policy, lambda: RiskPolicy())
        with TestClient(app) as client:
            expected = client.get(f"/api/v1/queues/{queue_id}/recommendations")
            assert expected.status_code == 200
            assessment_response = client.get(f"/api/v1/queues/{queue_id}/risk-assessment")
            assert expected.json()["assessment"] == assessment_response.json()
            listed = client.get("/api/v1/queues").json()
            assert {**identities[0], "sla_minutes": 30} in listed
    result = execute_tool("get_queue_recommendations", {"queue_id": queue_id},
                          access_for(identities, selected_queue_id=queue_id), engine=engine)
    assert result == expected.json()
    assert QueueRecommendations.model_validate(result).model_dump(mode="json") == result
    assert len(result["recommendations"]) == 4
    assert result["assessment"]["metrics"]["active_tickets"] == 2


@pytest.mark.parametrize("ticket_count", [0, 20])
def test_single_pipeline_and_no_individual_ticket_data_or_duplicate_aggregation(tool_data, ticket_count):
    engine, identities = tool_data
    queue_id = identities[0]["id"]
    with Session(engine) as seed:
        for index in range(ticket_count):
            seed.add(Ticket(external_id=f"EXTRA-{index}", subject="PRIVATE EXTRA SUBJECT",
                            queue_id=queue_id, priority="low", status="open", created_at=NOW))
        seed.commit()
    sql = []

    def record(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement)

    policy = RiskPolicy(sla_remaining_percent=35)
    event.listen(engine, "before_cursor_execute", record)
    try:
        with (
            patch.object(tools, "get_evaluation_time", return_value=NOW) as clock,
            patch.object(tools, "get_risk_policy", return_value=policy) as policy_getter,
            patch.object(assessment, "collect_queue_risk_metrics", wraps=assessment.collect_queue_risk_metrics) as collector,
            patch.object(assessment, "evaluate_risk", wraps=assessment.evaluate_risk) as evaluator,
            patch.object(queue_services, "build_recommendations", wraps=queue_services.build_recommendations) as builder,
        ):
            result = execute_tool("get_queue_recommendations", {"queue_id": queue_id},
                                  access_for(identities), engine=engine)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    clock.assert_called_once_with()
    policy_getter.assert_called_once_with()
    collector.assert_called_once()
    evaluator.assert_called_once()
    builder.assert_called_once()
    assert collector.call_args.args[1:] == (queue_id, NOW, policy)
    metrics, effective_policy, window = evaluator.call_args.args
    assert builder.call_args.args[:3] == (metrics, effective_policy, window)
    assert builder.call_args.args[0] is metrics
    assert builder.call_args.args[1] is policy
    with pytest.raises(FrozenInstanceError):
        metrics.active_tickets = 0
    assert len([statement for statement in sql if "FROM queues" in statement]) == 1
    aggregates = [statement for statement in sql if "FROM tickets" in statement]
    assert len(aggregates) == 1
    assert "count(" in aggregates[0]
    assert "tickets.subject" not in aggregates[0]
    assert "tickets.external_id" not in aggregates[0]
    assert "agents.name" not in aggregates[0]
    assert "PRIVATE" not in json.dumps(result)
    assert f"{identities[0]['name'].split()[0]}-1" not in json.dumps(result)
    assert "EXTRA-" not in json.dumps(result)
    assert "subject" not in json.dumps(result) and "ticket_id" not in json.dumps(result)


@pytest.mark.parametrize("operation", ["flush", "commit", "insert", "update", "delete", "ddl", "raw"])
def test_explicit_writes_blocked_and_business_rows_unchanged(tool_data, operation):
    engine, identities = tool_data
    before = business_rows(engine)
    with read_only_session(engine) as db:
        assert db.autoflush is False
        with pytest.raises(ReadOnlyViolation):
            if operation in {"flush", "commit"}:
                getattr(db, operation)()
            elif operation == "insert":
                db.execute(Queue.__table__.insert().values(name="ILLEGAL", sla_minutes=1))
            elif operation == "update":
                db.execute(Queue.__table__.update().values(name="ILLEGAL"))
            elif operation == "delete":
                db.execute(delete(Ticket))
            elif operation == "ddl":
                db.execute(text("CREATE TABLE illegal (id INTEGER)"))
            else:
                db.connection().exec_driver_sql("DELETE FROM tickets")
    assert business_rows(engine) == before


def test_pending_objects_do_not_autoflush_and_sessions_do_not_escape(tool_data):
    engine, identities = tool_data
    before = business_rows(engine)
    with read_only_session(engine) as db:
        queue = db.get(Queue, identities[0]["id"])
        queue.name = "PENDING NAME"
        db.add(Queue(name="PENDING INSERT", sla_minutes=1))
        with patch.object(db, "flush", side_effect=AssertionError("autoflush")):
            listed = queue_services.list_queues(db, allowed_queue_ids=access_for(identities).allowed_queue_ids)
            context = queue_services.assess_queue(db, queue.id, NOW, RiskPolicy())
            result = queue_services.recommendations_response(context)
        assert listed[0].name == identities[0]["name"]
        assert result.assessment.queue_name == identities[0]["name"]
        assert not any(isinstance(obj, (Ticket, Agent)) for obj in db.identity_map.values())
    assert not db.identity_map and not db.new
    assert business_rows(engine) == before


@pytest.mark.parametrize("statement", [
    "INSERT INTO queues (name, sla_minutes) VALUES ('READONLY-PROBE', 30)",
    "UPDATE queues SET sla_minutes = sla_minutes + 1",
    "DELETE FROM tickets",
], ids=["insert", "update", "delete"])
def test_native_database_read_only_and_pool_mode_restored(tool_data, statement):
    engine, identities = tool_data
    before = business_rows(engine)
    with read_only_session(engine, statement_timeout_ms=1234) as db:
        conn = db.connection()
        raw = conn.connection.driver_connection
        with closing(raw.cursor()) as cursor:
            if engine.dialect.name == "postgresql":
                cursor.execute("SHOW transaction_read_only")
                assert cursor.fetchone()[0] == "on"
                cursor.execute("SHOW statement_timeout")
                assert cursor.fetchone()[0] == "1234ms"
            else:
                cursor.execute("PRAGMA query_only")
                assert cursor.fetchone()[0] == 1
            # Bypass the Python guard to prove that the database itself rejects DML.
            with pytest.raises((ReadOnlySqlTransaction, SQLiteOperationalError)) as exc:
                cursor.execute(statement)
            if engine.dialect.name == "postgresql":
                assert exc.value.sqlstate == "25006"
            else:
                assert "readonly" in str(exc.value).lower()
    if engine.dialect.name == "postgresql":
        assert raw.info.transaction_status == TransactionStatus.IDLE
    else:
        assert not raw.in_transaction
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            assert conn.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "off"
        else:
            assert conn.exec_driver_sql("PRAGMA query_only").scalar_one() == 0
    assert business_rows(engine) == before


def test_existing_write_transactions_are_never_reconfigured(session):
    queue = Queue(name="Pending fixture writes", sla_minutes=1)
    session.add(queue)
    session.flush()
    with pytest.raises(TypeError, match="never an existing session"):
        with read_only_session(session):
            pytest.fail("accepted fixture session")
    with pytest.raises(TypeError):
        with read_only_session(session.connection()):
            pytest.fail("accepted fixture connection")
    assert session.scalar(select(Queue.id).where(Queue.name == queue.name)) == queue.id


@pytest.mark.parametrize("failure", [None, "missing", "query", "result", "limit"])
def test_sessions_close_and_connections_return_on_success_and_failure(tool_data, monkeypatch, failure):
    engine, identities = tool_data
    queue_id = identities[0]["id"]
    opened, closed = [], []
    checkouts, checkins = [], []

    class TrackingSession(read_only.ReadOnlySession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            opened.append(self)

        def close(self):
            super().close()
            closed.append(self)

    def checkout(*args):
        checkouts.append(True)

    def checkin(*args):
        checkins.append(True)

    monkeypatch.setattr(read_only, "ReadOnlySession", TrackingSession)
    if failure == "query":
        monkeypatch.setattr(queue_services, "assess_queue", lambda *a: (_ for _ in ()).throw(
            OperationalError("PRIVATE SQL", {}, Exception("PRIVATE HOST"))))
    elif failure == "result":
        def malformed(context):
            return SimpleNamespace(model_dump_json=lambda: '{"private":"SECRET"}')
        monkeypatch.setattr(queue_services, "recommendations_response", malformed)
    if failure == "missing":
        queue_id = 999_999
        identities = [{"id": queue_id}]
    access = access_for(identities, limits=ExecutionLimits(max_result_bytes=1) if failure == "limit" else ExecutionLimits())
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        if failure:
            with pytest.raises(ToolError) as exc:
                execute_tool("get_queue_recommendations", {"queue_id": queue_id}, access, engine=engine)
            assert "PRIVATE" not in str(exc.value) and "SECRET" not in str(exc.value)
        else:
            snapshot = execute_tool("get_queue_recommendations", {"queue_id": queue_id}, access, engine=engine)
            assert snapshot["assessment"]["queue_id"] == queue_id
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
    assert len(opened) == 1 and opened == closed
    assert checkouts == checkins == [True]
    assert not opened[0].identity_map


def test_detached_snapshots_and_strict_result_validation(tool_data):
    engine, identities = tool_data
    args = {"queue_id": identities[0]["id"]}
    result = execute_tool("get_queue_recommendations", args, access_for(identities), engine=engine)
    assert json.loads(json.dumps(result)) == result
    validated = QueueRecommendationsResult.model_validate_json(json.dumps(result), strict=True)
    result["assessment"]["metrics"]["active_tickets"] = 999
    assert validated.assessment.metrics.active_tickets == 2
    for path in ((), ("assessment",), ("assessment", "metrics"), ("assessment", "policy")):
        bad = validated.model_dump(mode="json")
        target = bad
        for key in path:
            target = target[key]
        target["private"] = "SECRET"
        with pytest.raises(ValidationError):
            QueueRecommendationsResult.model_validate_json(json.dumps(bad), strict=True)
    bad = validated.model_dump(mode="json")
    bad["assessment"]["metrics"]["active_tickets"] = "2"
    with pytest.raises(ValidationError):
        QueueRecommendationsResult.model_validate_json(json.dumps(bad), strict=True)


def test_tools_make_no_http_or_provider_calls(tool_data):
    engine, identities = tool_data
    with (
        patch.object(httpx.Client, "send", side_effect=AssertionError("HTTP call")),
        patch.object(socket, "create_connection", side_effect=AssertionError("external connection")),
    ):
        execute_tool("list_queues", {}, access_for(identities), engine=engine)
        execute_tool("get_queue_recommendations", {"queue_id": identities[0]["id"]},
                     access_for(identities), engine=engine)
    assert tools.SUPPORTED_TOOLS == frozenset({"list_queues", "get_queue_recommendations"})
    assert not any("agent" in route.path for route in app.routes if route.path.startswith("/api/") and route.path != "/api/v1/agents")


def test_transaction_safety_setup_precedes_business_queries(tool_data):
    engine, identities = tool_data
    statements = []

    def record(conn, cursor, sql, *args):
        statements.append(sql)

    event.listen(engine, "before_cursor_execute", record)
    try:
        execute_tool("list_queues", {}, access_for(identities), engine=engine)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    if engine.dialect.name == "postgresql":
        assert statements[0] == "SET TRANSACTION READ ONLY"
        assert statements[1] == "SET LOCAL statement_timeout = 5000"
    else:
        assert statements[:2] == ["PRAGMA query_only", "PRAGMA query_only = ON"]
    assert "SELECT queues.id" in statements[2]


def test_transaction_setup_failure_returns_connection(tool_data):
    engine, identities = tool_data
    checkins = []

    def fail_setup(conn, cursor, sql, *args):
        if sql.startswith(("SET TRANSACTION", "PRAGMA query_only")):
            raise OperationalError("PRIVATE SQL", {}, Exception("PRIVATE CONNECTION"))

    def checkin(*args):
        checkins.append(True)

    event.listen(engine, "before_cursor_execute", fail_setup)
    event.listen(engine, "checkin", checkin)
    try:
        with pytest.raises(ToolError, match="^database unavailable$"):
            execute_tool("list_queues", {}, access_for(identities), engine=engine)
    finally:
        event.remove(engine, "before_cursor_execute", fail_setup)
        event.remove(engine, "checkin", checkin)
    assert checkins == [True]
    assert execute_tool("list_queues", {}, access_for(identities), engine=engine) == identities


def test_connection_failure_is_sanitized():
    username, password = uuid4().hex, uuid4().hex
    private_url = URL.create("postgresql+psycopg", username=username, password=password,
                             host="private.invalid", database="private_test").render_as_string(hide_password=False)

    def unavailable():
        raise OperationalError("PRIVATE SQL", {"url": private_url}, Exception(private_url))

    engine = create_engine("sqlite+pysqlite://", creator=unavailable)
    try:
        with pytest.raises(ToolError, match="^database unavailable$") as exc:
            execute_tool("list_queues", {}, access_for([{"id": 1}]), engine=engine)
        for private in (username, password, private_url):
            assert private not in str(exc.value) and private not in repr(exc.value)
    finally:
        engine.dispose()


def test_strict_boolean_result_flags(tool_data):
    engine, identities = tool_data
    result = execute_tool("get_queue_recommendations", {"queue_id": identities[0]["id"]},
                          access_for(identities), engine=engine)
    for field in ("advisory_only", "requires_human_review"):
        with pytest.raises(ValidationError):
            QueueRecommendationsResult.model_validate_json(json.dumps({**result, field: 1}), strict=True)


def test_recommendation_result_must_match_requested_queue(tool_data, monkeypatch):
    engine, identities = tool_data
    original = queue_services.recommendations_response

    def wrong_queue(context):
        result = original(context)
        result.assessment.queue_id = identities[1]["id"]
        return result

    monkeypatch.setattr(queue_services, "recommendations_response", wrong_queue)
    with pytest.raises(ToolError, match="^invalid tool result$"):
        execute_tool("get_queue_recommendations", {"queue_id": identities[0]["id"]},
                     access_for(identities), engine=engine)


def test_postgresql_autocommit_engine_cannot_bypass_read_only(tool_data):
    engine, identities = tool_data
    if engine.dialect.name != "postgresql":
        pytest.skip("checks PostgreSQL transaction mode with an autocommit engine")
    with read_only_session(engine.execution_options(isolation_level="AUTOCOMMIT")) as db:
        with closing(db.connection().connection.driver_connection.cursor()) as cursor:
            cursor.execute("SHOW transaction_read_only")
            assert cursor.fetchone()[0] == "on"
            with pytest.raises(Exception) as exc:
                cursor.execute("DELETE FROM tickets")
            assert exc.value.sqlstate == "25006"


def test_sqlite_preserves_preexisting_query_only_setting(tool_data):
    engine, identities = tool_data
    if engine.dialect.name != "sqlite":
        pytest.skip("checks SQLite connection-wide query_only restoration")
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA query_only = ON")
    try:
        with read_only_session(engine) as db:
            assert queue_services.list_queues(db)
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
    finally:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA query_only = OFF")


def test_discovery_result_must_stay_within_scope(tool_data, monkeypatch):
    engine, identities = tool_data
    monkeypatch.setattr(queue_services, "list_queues", lambda *a, **kw: [
        SimpleNamespace(id=identities[1]["id"], name=identities[1]["name"]),
    ])
    with pytest.raises(ToolError, match="^invalid tool result$"):
        execute_tool("list_queues", {}, access_for(identities[:1]), engine=engine)
    with pytest.raises(ToolError, match="^invalid tool result$"):
        execute_tool("list_queues", {}, access_for(identities, selected_queue_id=identities[0]["id"]), engine=engine)


def test_sqlite_failed_restoration_discards_connection(tmp_path):
    # A file-backed database survives replacement of an unsafe pooled connection.
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'pool-review.db'}",
                           pool_size=1, max_overflow=0)
    with engine.begin() as conn:
        Queue.__table__.create(conn)
        queue_id = conn.execute(Queue.__table__.insert().values(
            name="Pool restoration fixture", sla_minutes=30,
        ).returning(Queue.id)).scalar_one()
    with engine.connect() as conn:
        original = conn.connection.driver_connection

    def fail_restore(conn, cursor, sql, *args):
        if sql == "PRAGMA query_only = 0":
            raise OperationalError("PRIVATE restoration SQL", {}, Exception("PRIVATE connection"))

    event.listen(engine, "before_cursor_execute", fail_restore)
    try:
        with pytest.raises(ToolError, match="^database unavailable$"):
            execute_tool("list_queues", {}, access_for([{"id": queue_id}]), engine=engine)
    finally:
        event.remove(engine, "before_cursor_execute", fail_restore)
    try:
        with engine.begin() as conn:
            assert conn.exec_driver_sql("PRAGMA query_only").scalar_one() == 0
            assert conn.connection.driver_connection is not original
            assert conn.scalar(select(Queue.sla_minutes).where(Queue.id == queue_id)) == 30
            conn.execute(Queue.__table__.update().where(Queue.id == queue_id).values(sla_minutes=31))
    finally:
        engine.dispose()


@pytest.mark.parametrize("failure", [None, "query", "result", "limit", "session"])
def test_write_api_works_on_same_connection_after_tools(tool_data, monkeypatch, failure):
    engine, identities = tool_data
    # StaticPool already shares one SQLite connection; use a one-connection PostgreSQL pool.
    pooled = engine if engine.dialect.name == "sqlite" else create_engine(
        engine.url, pool_size=1, max_overflow=0,
    )
    borrowed = []

    def checkout(dbapi_connection, *args):
        borrowed.append(dbapi_connection)

    def api_session():
        with Session(pooled) as db:
            yield db

    event.listen(pooled, "checkout", checkout)
    try:
        queue_id = identities[0]["id"]
        before = business_rows(engine)
        limits = ExecutionLimits(max_result_bytes=1) if failure == "limit" else ExecutionLimits()
        with monkeypatch.context() as scoped:
            if failure in {"query", "session"}:
                def unavailable(*args, **kwargs):
                    raise OperationalError("PRIVATE SQL", {}, Exception("PRIVATE HOST"))
                if failure == "query":
                    scoped.setattr(queue_services, "assess_queue", unavailable)
                else:
                    scoped.setattr(read_only, "ReadOnlySession", unavailable)
            elif failure == "result":
                scoped.setattr(queue_services, "recommendations_response", lambda *a: SimpleNamespace(
                    model_dump_json=lambda: '{"private":"SECRET"}',
                ))
            if failure:
                with pytest.raises(ToolError):
                    execute_tool("get_queue_recommendations", {"queue_id": queue_id},
                                 access_for(identities, limits=limits), engine=pooled)
            else:
                execute_tool("list_queues", {}, access_for(identities), engine=pooled)
                execute_tool("get_queue_recommendations", {"queue_id": queue_id},
                             access_for(identities), engine=pooled)
        tool_connection = borrowed[-1]
        assert business_rows(engine) == before
        with pooled.connect() as conn:
            assert conn.connection.driver_connection is tool_connection
            if pooled.dialect.name == "sqlite":
                assert conn.exec_driver_sql("PRAGMA query_only").scalar_one() == 0
            else:
                assert conn.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "off"
        monkeypatch.setitem(app.dependency_overrides, get_session, api_session)
        with TestClient(app) as client:
            payload = {"external_id": f"REUSE-{uuid4().hex[:16]}", "subject": "Write regression",
                       "queue_id": queue_id, "priority": "high"}
            created = client.post("/api/v1/tickets", json=payload)
            assert created.status_code == 201
            # Cover API rollback as well as both normal status transitions after tool cleanup.
            assert client.post("/api/v1/tickets", json=payload).status_code == 409
            ticket_id = created.json()["id"]
            for status in ("in_progress", "resolved"):
                response = client.patch(f"/api/v1/tickets/{ticket_id}/status", json={"status": status})
                assert response.status_code == 200
                assert response.json()["status"] == status
        # Every tool and API checkout uses the same physical connection.
        # Ignore baseline reads on the original engine when it is also the SQLite pool.
        assert borrowed and all(conn is tool_connection for conn in borrowed)
    finally:
        event.remove(pooled, "checkout", checkout)
        if pooled is not engine:
            pooled.dispose()
