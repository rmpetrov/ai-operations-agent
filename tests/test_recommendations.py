from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import count
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ai_operations_agent import assessment as assessment_module
from ai_operations_agent import main as main_module
from ai_operations_agent.clock import get_evaluation_time
from ai_operations_agent.config import get_risk_policy
from ai_operations_agent.db import get_session
from ai_operations_agent.main import app
from ai_operations_agent.models import Agent, Queue, Ticket
from ai_operations_agent.risk import RiskPolicy
from ai_operations_agent.schemas import QueueRecommendations

pytestmark = pytest.mark.usefixtures("evaluation_time")
_ticket_ids = count()

EXPECTED_RECOMMENDATIONS = [
    {
        "code": "REVIEW_OVERDUE_TICKETS", "priority": "high",
        "triggered_by_signals": ["SLA_BREACHED"],
        "suggested_action": "Review overdue active tickets and decide the next response.",
        "explanation": "1 active ticket is past its SLA deadline.",
        "evidence": {"sla_breached_tickets": 1},
    },
    {
        "code": "REVIEW_UPCOMING_DEADLINES", "priority": "medium",
        "triggered_by_signals": ["SLA_AT_RISK"],
        "suggested_action": "Review active tickets due now or within the warning window.",
        "explanation": "1 active ticket is due now or within 360 seconds.",
        "evidence": {"sla_at_risk_tickets": 1, "sla_at_risk_window_seconds": 360},
    },
    {
        "code": "VERIFY_RECORDED_AGENT_STATUS", "priority": "medium",
        "triggered_by_signals": ["NO_AGENTS_ONLINE"],
        "suggested_action": "Verify recorded agent status and confirm actual availability.",
        "explanation": (
            "No agents are recorded online for this queue's 2 active tickets. "
            "Recorded status does not establish actual availability."
        ),
        "evidence": {"active_tickets": 2, "online_agents": 0},
    },
    {
        "code": "TRIAGE_UNASSIGNED_TICKETS", "priority": "low",
        "triggered_by_signals": ["UNASSIGNED_BACKLOG"],
        "suggested_action": (
            "Review unassigned active tickets and decide whether assignment is appropriate."
        ),
        "explanation": "2 active tickets have no assigned agent.",
        "evidence": {"unassigned_tickets": 2, "unassigned_backlog_min_tickets": 1},
    },
]


@pytest.fixture
def recommendation_queue(session):
    queue = Queue(name="Recommendation queue", sla_minutes=30)
    session.add(queue)
    session.commit()
    return queue


def add_agent(session, queue, status="online"):
    agent = Agent(name="Recommendation agent", queue_id=queue.id, status=status)
    session.add(agent)
    session.flush()
    return agent


def add_ticket(session, queue, created_at, status="open", agent=None):
    ticket = Ticket(
        external_id=f"REC-{next(_ticket_ids)}", subject="Recommendation fixture",
        queue_id=queue.id, status=status, created_at=created_at,
        assigned_agent_id=agent.id if agent else None,
        resolved_at=created_at + timedelta(minutes=1) if status == "resolved" else None,
    )
    session.add(ticket)
    return ticket


def recommend(client, session, queue):
    session.commit()
    response = client.get(f"/api/v1/queues/{queue.id}/recommendations")
    assert response.status_code == 200
    return response.json()


@pytest.fixture
def all_signals_response(client, session, recommendation_queue, evaluation_time):
    add_agent(session, recommendation_queue, "offline")
    add_ticket(session, recommendation_queue, evaluation_time - timedelta(minutes=31))
    add_ticket(session, recommendation_queue, evaluation_time - timedelta(minutes=25), "in_progress")
    return recommend(client, session, recommendation_queue)


def test_exact_api_contract_and_sprint_2_regression(client, recommendation_queue, all_signals_response):
    expected_assessment = {
        "queue_id": recommendation_queue.id,
        "queue_name": "Recommendation queue",
        "evaluated_at": "2026-10-31T16:00:00.123456Z",
        "sla_minutes": 30,
        "policy": {
            "version": "queue-risk-v1", "sla_at_risk_remaining_percent": 20,
            "sla_at_risk_window_seconds": 360, "unassigned_backlog_min_tickets": 1,
        },
        "metrics": {
            "active_tickets": 2, "unassigned_tickets": 2, "sla_breached_tickets": 1,
            "sla_at_risk_tickets": 1, "online_agents": 0, "future_created_active_tickets": 0,
        },
        "overall_severity": "high",
        "signals": [
            {"code": "SLA_BREACHED", "severity": "high", "affected_tickets": 1,
             "reason": "1 active ticket is past its SLA deadline."},
            {"code": "SLA_AT_RISK", "severity": "medium", "affected_tickets": 1,
             "reason": "1 active ticket is due now or within 360 seconds."},
            {"code": "UNASSIGNED_BACKLOG", "severity": "low", "affected_tickets": 2,
             "reason": "2 active tickets have no assigned agent."},
            {"code": "NO_AGENTS_ONLINE", "severity": "medium", "affected_tickets": 2,
             "reason": "No agents are recorded online for this queue's 2 active tickets."},
        ],
    }
    assert all_signals_response == {
        "recommendation_policy_version": "queue-recommendations-v1",
        "advisory_only": True, "requires_human_review": True,
        "assessment": expected_assessment, "recommendations": EXPECTED_RECOMMENDATIONS,
    }
    assert QueueRecommendations.model_validate(all_signals_response).model_dump(mode="json") == all_signals_response
    original = client.get(f"/api/v1/queues/{recommendation_queue.id}/risk-assessment")
    assert original.status_code == 200
    assert original.json() == expected_assessment
    assert client.get(f"/api/v1/queues/{recommendation_queue.id}/recommendations").json() == all_signals_response


@pytest.mark.parametrize(("scenario", "index"), [("overdue", 0), ("upcoming", 1), ("recorded", 2), ("unassigned", 3)])
def test_each_api_trigger(client, session, recommendation_queue, evaluation_time, scenario, index):
    agent = add_agent(session, recommendation_queue, "offline" if scenario == "recorded" else "online")
    age = {"overdue": 31, "upcoming": 25}.get(scenario, 1)
    add_ticket(session, recommendation_queue, evaluation_time - timedelta(minutes=age),
               agent=None if scenario == "unassigned" else agent)
    body = recommend(client, session, recommendation_queue)
    assert [item["code"] for item in body["recommendations"]] == [EXPECTED_RECOMMENDATIONS[index]["code"]]
    assert body["recommendations"][0]["triggered_by_signals"] == [body["assessment"]["signals"][0]["code"]]


@pytest.mark.parametrize("resolved_only", [False, True])
@pytest.mark.parametrize("online_count", [0, 2])
def test_empty_and_resolved_only(client, session, recommendation_queue, evaluation_time, resolved_only, online_count):
    for _ in range(online_count):
        add_agent(session, recommendation_queue)
    if resolved_only:
        add_ticket(session, recommendation_queue, evaluation_time - timedelta(days=1), "resolved")
    body = recommend(client, session, recommendation_queue)
    assert body["recommendations"] == []
    assert body["assessment"]["signals"] == []
    assert body["assessment"]["overall_severity"] == "none"
    assert body["assessment"]["metrics"] == {
        "active_tickets": 0, "unassigned_tickets": 0, "sla_breached_tickets": 0,
        "sla_at_risk_tickets": 0, "online_agents": online_count, "future_created_active_tickets": 0,
    }
    assert body["advisory_only"] is True
    assert body["requires_human_review"] is True


def test_nonempty_queue_without_signals(client, session, recommendation_queue, evaluation_time):
    agent = add_agent(session, recommendation_queue)
    add_ticket(session, recommendation_queue, evaluation_time - timedelta(minutes=1), agent=agent)
    body = recommend(client, session, recommendation_queue)
    assert body["assessment"]["metrics"]["active_tickets"] == 1
    assert body["assessment"]["signals"] == []
    assert body["recommendations"] == []


@pytest.mark.parametrize("status", ["open", "in_progress"])
@pytest.mark.parametrize(("offset", "index"), [(-1, 0), (0, 1), (1, 1), (359_999_999, 1), (360_000_000, 1), (360_000_001, None)])
def test_sla_microsecond_boundaries(client, session, recommendation_queue, evaluation_time, status, offset, index):
    agent = add_agent(session, recommendation_queue)
    created = evaluation_time - timedelta(minutes=30) + timedelta(microseconds=offset)
    add_ticket(session, recommendation_queue, created, status, agent)
    body = recommend(client, session, recommendation_queue)
    assert body["recommendations"] == ([] if index is None else [EXPECTED_RECOMMENDATIONS[index]])


@pytest.mark.parametrize(("percent", "window"), [(20, 12), (1, 0.6), (99, 59.4)])
@pytest.mark.parametrize(("offset", "triggered"), [(-1, True), (0, True), (1, False)])
def test_nondefault_warning_policy(client, session, recommendation_queue, evaluation_time, monkeypatch, percent, window, offset, triggered):
    recommendation_queue.sla_minutes = 1
    policy = RiskPolicy(sla_remaining_percent=percent)
    monkeypatch.setitem(app.dependency_overrides, get_risk_policy, lambda: policy)
    agent = add_agent(session, recommendation_queue)
    created = evaluation_time - timedelta(minutes=1) + timedelta(seconds=window, microseconds=offset)
    add_ticket(session, recommendation_queue, created, agent=agent)
    body = recommend(client, session, recommendation_queue)
    assert body["assessment"]["policy"]["sla_at_risk_remaining_percent"] == percent
    assert body["assessment"]["policy"]["sla_at_risk_window_seconds"] == window
    assert bool(body["recommendations"]) is triggered
    if triggered:
        assert body["recommendations"][0]["evidence"] == {
            "sla_at_risk_tickets": 1, "sla_at_risk_window_seconds": window,
        }
        assert body["recommendations"][0]["explanation"] == f"1 active ticket is due now or within {window:g} seconds."


@pytest.mark.parametrize(("ticket_count", "triggered"), [(0, False), (1, False), (2, True), (3, True)])
def test_nondefault_unassigned_threshold(client, session, recommendation_queue, evaluation_time, monkeypatch, ticket_count, triggered):
    monkeypatch.setitem(app.dependency_overrides, get_risk_policy, lambda: RiskPolicy(unassigned_min_tickets=2))
    add_agent(session, recommendation_queue)
    for _ in range(ticket_count):
        add_ticket(session, recommendation_queue, evaluation_time)
    body = recommend(client, session, recommendation_queue)
    assert bool(body["recommendations"]) is triggered
    if triggered:
        assert body["recommendations"][0]["evidence"] == {
            "unassigned_tickets": ticket_count, "unassigned_backlog_min_tickets": 2,
        }


@pytest.mark.parametrize("status", ["open", "in_progress"])
def test_future_created_tickets(client, session, recommendation_queue, evaluation_time, status):
    add_ticket(session, recommendation_queue, evaluation_time + timedelta(microseconds=1), status)
    add_ticket(session, recommendation_queue, evaluation_time + timedelta(days=1), status)
    add_ticket(session, recommendation_queue, evaluation_time + timedelta(days=2), "resolved")
    body = recommend(client, session, recommendation_queue)
    assert body["assessment"]["metrics"] == {
        "active_tickets": 2, "unassigned_tickets": 2, "sla_breached_tickets": 0,
        "sla_at_risk_tickets": 0, "online_agents": 0, "future_created_active_tickets": 2,
    }
    assert body["recommendations"] == EXPECTED_RECOMMENDATIONS[2:]


def test_offline_assignment_and_other_queue_presence(client, session, recommendation_queue, evaluation_time):
    other = Queue(name="Other recommendation queue", sla_minutes=1)
    session.add(other)
    session.flush()
    for _ in range(3):
        add_agent(session, other)
        add_ticket(session, other, evaluation_time - timedelta(days=1))
    offline = add_agent(session, recommendation_queue, "offline")
    for _ in range(2):
        add_ticket(session, recommendation_queue, evaluation_time, agent=offline)
    body = recommend(client, session, recommendation_queue)
    assert body["recommendations"] == [EXPECTED_RECOMMENDATIONS[2]]
    assert body["assessment"]["metrics"]["unassigned_tickets"] == 0
    add_agent(session, recommendation_queue)
    assert recommend(client, session, recommendation_queue)["recommendations"] == []


def test_resolving_ticket_removes_recommendations(client, session, recommendation_queue, evaluation_time):
    ticket = add_ticket(session, recommendation_queue, evaluation_time - timedelta(hours=1))
    assert recommend(client, session, recommendation_queue)["recommendations"]
    assert client.patch(f"/api/v1/tickets/{ticket.id}/status", json={"status": "resolved"}).status_code == 200
    assert recommend(client, session, recommendation_queue)["recommendations"] == []


def test_one_pipeline_and_same_clock_policy_and_facts(client, session, recommendation_queue, evaluation_time, monkeypatch):
    policy = RiskPolicy(sla_remaining_percent=35, unassigned_min_tickets=2)
    timestamp = evaluation_time.astimezone(timezone(timedelta(hours=5, minutes=30)))
    session.commit()
    with (
        patch.object(assessment_module, "collect_queue_risk_metrics", wraps=assessment_module.collect_queue_risk_metrics) as collector,
        patch.object(assessment_module, "evaluate_risk", wraps=assessment_module.evaluate_risk) as evaluator,
        patch.object(main_module, "build_recommendations", wraps=main_module.build_recommendations) as builder,
    ):
        clock_calls, policy_calls = [], []

        def clock():
            clock_calls.append(True)
            return timestamp

        def effective_policy():
            policy_calls.append(True)
            return policy

        monkeypatch.setitem(app.dependency_overrides, get_evaluation_time, clock)
        monkeypatch.setitem(app.dependency_overrides, get_risk_policy, effective_policy)
        body = client.get(f"/api/v1/queues/{recommendation_queue.id}/recommendations").json()
    assert clock_calls == policy_calls == [True]
    collector.assert_called_once_with(session, recommendation_queue.id, evaluation_time, policy)
    evaluator.assert_called_once()
    builder.assert_called_once()
    metrics, effective, window = evaluator.call_args.args
    built_metrics, built_policy, built_window, evaluation = builder.call_args.args
    assert built_metrics is metrics
    assert effective is built_policy is policy
    assert built_window == window == 630
    assert evaluation.signals == ()
    assert body["assessment"]["evaluated_at"] == "2026-10-31T16:00:00.123456Z"


@pytest.mark.parametrize("ticket_count", [1, 20])
def test_no_writes_autoflush_or_repeated_business_queries(client, session, recommendation_queue, evaluation_time, ticket_count):
    agent = add_agent(session, recommendation_queue)
    for _ in range(ticket_count):
        add_ticket(session, recommendation_queue, evaluation_time - timedelta(hours=1), agent=agent)
    session.commit()
    queue_id = recommendation_queue.id
    tables = [Queue.__table__, Agent.__table__, Ticket.__table__]
    before = [session.execute(select(table).order_by(table.c.id)).all() for table in tables]
    recommendation_queue.name = "Pending queue update"
    agent.status = "offline"
    statements = []
    connection = session.connection()

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", record_sql)
    try:
        with (
            patch.object(session, "flush", side_effect=AssertionError("unexpected flush")),
            patch.object(session, "commit", side_effect=AssertionError("unexpected commit")),
            patch.object(session, "add", side_effect=AssertionError("unexpected add")),
            patch.object(session, "delete", side_effect=AssertionError("unexpected delete")),
        ):
            response = client.get(f"/api/v1/queues/{queue_id}/recommendations")
        assert response.status_code == 200
        assert response.json()["assessment"]["queue_name"] == "Recommendation queue"
        assert response.json()["assessment"]["metrics"]["online_agents"] == 1
        assert [item["code"] for item in response.json()["recommendations"]] == ["REVIEW_OVERDUE_TICKETS"]
    finally:
        event.remove(connection, "before_cursor_execute", record_sql)
        session.expire_all()
    # Ignore connection/framework initialization; inspect only reads of business tables.
    business = [sql for sql in statements if any(name in sql.lower() for name in ("queues", "tickets", "agents"))]
    assert len(business) == 2
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    assert sum("count(" in sql.lower() for sql in business) == 1
    assert [session.execute(select(table).order_by(table.c.id)).all() for table in tables] == before


@pytest.mark.parametrize("queue_id", [999_999, 0, -1])
def test_missing_queue(client, queue_id):
    response = client.get(f"/api/v1/queues/{queue_id}/recommendations")
    assert response.status_code == 404
    assert response.json() == {"detail": "queue not found"}


@pytest.mark.parametrize("queue_id", ["abc", "1.5"])
def test_invalid_path(client, queue_id):
    response = client.get(f"/api/v1/queues/{queue_id}/recommendations")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["path", "queue_id"]


@pytest.mark.parametrize("failing_query", [1, 2])
def test_database_failure_is_sanitized(client, session, recommendation_queue, monkeypatch, failing_query):
    queue_id = recommendation_queue.id
    original = session.execute
    calls = 0

    def execute(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == failing_query:
            raise OperationalError("private SQL", {"password": "private"}, Exception("private host"))
        return original(*args, **kwargs)

    monkeypatch.setattr(session, "execute", execute)
    response = client.get(f"/api/v1/queues/{queue_id}/recommendations")
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}


def test_database_connection_failure_is_sanitized(client, monkeypatch):
    def unavailable():
        raise OperationalError("private SQL", {}, Exception("private connection"))

    engine = create_engine("sqlite+pysqlite://", creator=unavailable)
    try:
        with Session(engine) as disconnected:
            monkeypatch.setitem(app.dependency_overrides, get_session, lambda: disconnected)
            response = client.get("/api/v1/queues/1/recommendations")
        assert response.status_code == 503
        assert response.json() == {"detail": "database unavailable"}
    finally:
        engine.dispose()


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
def test_recommendation_route_supports_only_reads(client, recommendation_queue, method):
    response = client.request(method, f"/api/v1/queues/{recommendation_queue.id}/recommendations")
    assert response.status_code == 405


@pytest.mark.parametrize("invalid", ["trigger", "count", "window", "minimum", "duplicate", "advisory", "review", "execution"])
def test_response_traceability_and_advisory_validation(all_signals_response, invalid):
    body = deepcopy(all_signals_response)
    if invalid == "trigger":
        body["assessment"]["signals"] = body["assessment"]["signals"][1:]
    elif invalid == "count":
        body["recommendations"][0]["evidence"]["sla_breached_tickets"] = 2
    elif invalid == "window":
        body["recommendations"][1]["evidence"]["sla_at_risk_window_seconds"] = 1
    elif invalid == "minimum":
        body["recommendations"][3]["evidence"]["unassigned_backlog_min_tickets"] = 2
    elif invalid == "duplicate":
        body["recommendations"][1] = body["recommendations"][0]
    elif invalid == "advisory":
        body["advisory_only"] = False
    elif invalid == "review":
        body["requires_human_review"] = False
    else:
        body["executed"] = True
    with pytest.raises(ValidationError):
        QueueRecommendations.model_validate(body)


def test_dst_offsets_and_invalid_internal_clock(client, session, recommendation_queue, monkeypatch):
    evaluated = datetime(2026, 11, 1, 1, 15, tzinfo=ZoneInfo("America/New_York"), fold=1)
    created = datetime(2026, 11, 1, 1, 45, tzinfo=ZoneInfo("America/New_York"), fold=0)
    agent = add_agent(session, recommendation_queue)
    stored = created if session.get_bind().dialect.name == "postgresql" else created.astimezone(timezone.utc)
    add_ticket(session, recommendation_queue, stored, agent=agent)
    monkeypatch.setitem(app.dependency_overrides, get_evaluation_time, lambda: evaluated)
    body = recommend(client, session, recommendation_queue)
    assert body["assessment"]["evaluated_at"] == "2026-11-01T06:15:00Z"
    assert body["recommendations"] == [EXPECTED_RECOMMENDATIONS[1]]
    monkeypatch.setitem(app.dependency_overrides, get_evaluation_time, lambda: datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        client.get(f"/api/v1/queues/{recommendation_queue.id}/recommendations")
