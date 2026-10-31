from datetime import datetime, timedelta, timezone
from itertools import count
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from ai_operations_agent.clock import get_evaluation_time, normalize_evaluation_time
from ai_operations_agent.config import get_risk_policy
from ai_operations_agent.main import app
from ai_operations_agent.metrics import collect_queue_risk_metrics
from ai_operations_agent.models import Agent, Queue, Ticket
from ai_operations_agent.risk import RiskPolicy
from ai_operations_agent.schemas import QueueRiskAssessment, RiskAssessmentMetrics

pytestmark = pytest.mark.usefixtures("evaluation_time")
_ticket_ids = count()


@pytest.fixture
def risk_queue(session):
    queue = Queue(name="Risk assessment queue", sla_minutes=30)
    session.add(queue)
    session.commit()
    return queue


def add_agent(session, queue, status="online"):
    agent = Agent(name="Risk agent", queue_id=queue.id, status=status)
    session.add(agent)
    session.flush()
    return agent


def add_ticket(session, queue, created_at, status="open", agent=None):
    ticket = Ticket(
        external_id=f"RISK-{queue.id}-{next(_ticket_ids)}",
        subject="Risk fixture",
        queue_id=queue.id,
        created_at=created_at,
        status=status,
        assigned_agent_id=agent.id if agent else None,
        resolved_at=created_at + timedelta(minutes=1) if status == "resolved" else None,
    )
    session.add(ticket)
    return ticket


def assess(client, session, queue):
    session.commit()
    response = client.get(f"/api/v1/queues/{queue.id}/risk-assessment")
    assert response.status_code == 200
    return response.json()


def test_exact_response_contract_and_overlapping_signals(
    client,
    session,
    risk_queue,
    evaluation_time,
):
    add_agent(session, risk_queue, "offline")
    add_ticket(session, risk_queue, evaluation_time - timedelta(minutes=31))
    add_ticket(session, risk_queue, evaluation_time - timedelta(minutes=25), "in_progress")
    body = assess(client, session, risk_queue)
    assert body == {
        "queue_id": risk_queue.id,
        "queue_name": risk_queue.name,
        "evaluated_at": "2026-10-31T16:00:00.123456Z",
        "sla_minutes": 30,
        "policy": {
            "version": "queue-risk-v1",
            "sla_at_risk_remaining_percent": 20,
            "sla_at_risk_window_seconds": 360,
            "unassigned_backlog_min_tickets": 1,
        },
        "metrics": {
            "active_tickets": 2,
            "unassigned_tickets": 2,
            "sla_breached_tickets": 1,
            "sla_at_risk_tickets": 1,
            "online_agents": 0,
            "future_created_active_tickets": 0,
        },
        "overall_severity": "high",
        "signals": [
            {
                "code": "SLA_BREACHED",
                "severity": "high",
                "affected_tickets": 1,
                "reason": "1 active ticket is past its SLA deadline.",
            },
            {
                "code": "SLA_AT_RISK",
                "severity": "medium",
                "affected_tickets": 1,
                "reason": "1 active ticket is due now or within 360 seconds.",
            },
            {
                "code": "UNASSIGNED_BACKLOG",
                "severity": "low",
                "affected_tickets": 2,
                "reason": "2 active tickets have no assigned agent.",
            },
            {
                "code": "NO_AGENTS_ONLINE",
                "severity": "medium",
                "affected_tickets": 2,
                "reason": "No agents are recorded online for this queue's 2 active tickets.",
            },
        ],
    }
    assert QueueRiskAssessment.model_validate(body).model_dump(mode="json") == body
    # Identical stored facts, clock, and policy produce exactly the same result.
    assert assess(client, session, risk_queue) == body


@pytest.mark.parametrize("resolved_only", [False, True])
@pytest.mark.parametrize("online_count", [0, 2])
def test_empty_or_resolved_only(
    client, session, risk_queue, evaluation_time, resolved_only, online_count
):
    for _ in range(online_count):
        add_agent(session, risk_queue)
    if resolved_only:
        add_ticket(session, risk_queue, evaluation_time - timedelta(days=1), "resolved")
    body = assess(client, session, risk_queue)
    assert body["signals"] == []
    assert body["overall_severity"] == "none"
    assert body["metrics"] == {
        "active_tickets": 0,
        "unassigned_tickets": 0,
        "sla_breached_tickets": 0,
        "sla_at_risk_tickets": 0,
        "online_agents": online_count,
        "future_created_active_tickets": 0,
    }


@pytest.mark.parametrize("status", ["open", "in_progress"])
@pytest.mark.parametrize(
    ("deadline_offset", "breached", "at_risk"),
    [
        (-1, 1, 0),
        (0, 0, 1),
        (1, 0, 1),
        (359_999_999, 0, 1),
        (360_000_000, 0, 1),
        (360_000_001, 0, 0),
    ],
)
def test_sla_microsecond_boundaries(
    client, session, risk_queue, evaluation_time, status, deadline_offset, breached, at_risk
):
    agent = add_agent(session, risk_queue)
    created_at = evaluation_time - timedelta(minutes=30) + timedelta(microseconds=deadline_offset)
    add_ticket(session, risk_queue, created_at, status, agent)
    body = assess(client, session, risk_queue)
    assert body["metrics"]["active_tickets"] == 1
    assert body["metrics"]["sla_breached_tickets"] == breached
    assert body["metrics"]["sla_at_risk_tickets"] == at_risk
    assert body["overall_severity"] == ("high" if breached else "medium" if at_risk else "none")
    assert [signal["code"] for signal in body["signals"]] == (
        ["SLA_BREACHED"] if breached else ["SLA_AT_RISK"] if at_risk else []
    )


def test_queue_agent_isolation_and_no_join_multiplication(
    client,
    session,
    risk_queue,
    evaluation_time,
):
    other = Queue(name="Other risk queue", sla_minutes=1)
    session.add(other)
    session.flush()
    for _ in range(3):
        add_agent(session, other)
    assigned = add_agent(session, risk_queue, "offline")
    for index in range(4):
        add_ticket(session, risk_queue, evaluation_time - timedelta(minutes=index), agent=assigned)
        add_ticket(session, other, evaluation_time - timedelta(hours=index + 1))
    body = assess(client, session, risk_queue)
    assert body["metrics"] == {
        "active_tickets": 4,
        "unassigned_tickets": 0,
        "sla_breached_tickets": 0,
        "sla_at_risk_tickets": 0,
        "online_agents": 0,
        "future_created_active_tickets": 0,
    }
    assert [signal["code"] for signal in body["signals"]] == ["NO_AGENTS_ONLINE"]
    for _ in range(2):
        add_agent(session, risk_queue)
    body = assess(client, session, risk_queue)
    assert body["metrics"]["active_tickets"] == 4
    assert body["metrics"]["online_agents"] == 2
    assert body["signals"] == []


@pytest.mark.parametrize(("count", "triggered"), [(0, False), (1, False), (2, True), (3, True)])
def test_effective_unassigned_threshold(
    client, session, risk_queue, evaluation_time, monkeypatch, count, triggered
):
    monkeypatch.setitem(
        app.dependency_overrides, get_risk_policy, lambda: RiskPolicy(unassigned_min_tickets=2)
    )
    add_agent(session, risk_queue)
    for index in range(count):
        add_ticket(session, risk_queue, evaluation_time - timedelta(seconds=index))
    body = assess(client, session, risk_queue)
    assert body["policy"]["unassigned_backlog_min_tickets"] == 2
    assert bool(body["signals"]) is triggered
    assert body["overall_severity"] == ("low" if triggered else "none")


@pytest.mark.parametrize(("percent", "window_seconds"), [(20, 12), (1, 0.6), (99, 59.4)])
@pytest.mark.parametrize(("offset", "at_risk"), [(-1, 1), (0, 1), (1, 0)])
def test_configurable_short_fractional_warning_window(
    client,
    session,
    risk_queue,
    evaluation_time,
    monkeypatch,
    percent,
    window_seconds,
    offset,
    at_risk,
):
    risk_queue.sla_minutes = 1
    monkeypatch.setitem(
        app.dependency_overrides, get_risk_policy, lambda: RiskPolicy(sla_remaining_percent=percent)
    )
    agent = add_agent(session, risk_queue)
    created = (
        evaluation_time
        - timedelta(minutes=1)
        + timedelta(seconds=window_seconds, microseconds=offset)
    )
    add_ticket(session, risk_queue, created, agent=agent)
    body = assess(client, session, risk_queue)
    assert body["policy"]["sla_at_risk_remaining_percent"] == percent
    assert body["policy"]["sla_at_risk_window_seconds"] == window_seconds
    assert body["metrics"]["sla_at_risk_tickets"] == at_risk
    if at_risk:
        assert body["signals"][0]["reason"] == (
            f"1 active ticket is due now or within {window_seconds:g} seconds."
        )


@pytest.mark.parametrize("status", ["open", "in_progress"])
def test_future_created_tickets_remain_backlog(
    client, session, risk_queue, evaluation_time, status
):
    add_ticket(session, risk_queue, evaluation_time + timedelta(microseconds=1), status)
    add_ticket(session, risk_queue, evaluation_time + timedelta(days=1), status)
    add_ticket(session, risk_queue, evaluation_time + timedelta(days=2), "resolved")
    body = assess(client, session, risk_queue)
    assert body["metrics"] == {
        "active_tickets": 2,
        "unassigned_tickets": 2,
        "sla_breached_tickets": 0,
        "sla_at_risk_tickets": 0,
        "online_agents": 0,
        "future_created_active_tickets": 2,
    }
    assert [signal["code"] for signal in body["signals"]] == [
        "UNASSIGNED_BACKLOG",
        "NO_AGENTS_ONLINE",
    ]
    assert body["overall_severity"] == "medium"


def test_resolved_ticket_excluded_on_next_request(client, session, risk_queue, evaluation_time):
    ticket = add_ticket(session, risk_queue, evaluation_time - timedelta(hours=1))
    assert assess(client, session, risk_queue)["overall_severity"] == "high"
    assert (
        client.patch(f"/api/v1/tickets/{ticket.id}/status", json={"status": "resolved"}).status_code
        == 200
    )
    body = assess(client, session, risk_queue)
    assert body["metrics"]["active_tickets"] == 0
    assert body["signals"] == []


@pytest.mark.parametrize("queue_id", [999_999, 0, -1])
def test_missing_queue(client, queue_id):
    response = client.get(f"/api/v1/queues/{queue_id}/risk-assessment")
    assert response.status_code == 404
    assert response.json() == {"detail": "queue not found"}


@pytest.mark.parametrize("queue_id", ["abc", "1.5"])
def test_invalid_path_id(client, queue_id):
    response = client.get(f"/api/v1/queues/{queue_id}/risk-assessment")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["path", "queue_id"]


@pytest.mark.parametrize("failing_query", [1, 2])
def test_database_failures_are_sanitized(client, session, risk_queue, monkeypatch, failing_query):
    original = session.execute
    calls = 0

    def failing_execute(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == failing_query:
            raise OperationalError(
                "private SQL", {"password": "private"}, Exception("private host")
            )
        return original(*args, **kwargs)

    queue_id = risk_queue.id
    monkeypatch.setattr(session, "execute", failing_execute)
    response = client.get(f"/api/v1/queues/{queue_id}/risk-assessment")
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}


def test_read_only_aggregate_queries_without_autoflush(
    client,
    session,
    risk_queue,
    evaluation_time,
):
    agent = add_agent(session, risk_queue)
    add_ticket(session, risk_queue, evaluation_time - timedelta(hours=1), agent=agent)
    session.commit()
    queue_id = risk_queue.id
    before = session.execute(select(Ticket.__table__)).all()
    # A pending dirty object must not cause an implicit UPDATE while reading metrics.
    risk_queue.name = "Pending name change"
    statements = []
    connection = session.connection()

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", record_sql)
    try:
        with (
            patch.object(session, "flush", side_effect=AssertionError("unexpected flush")),
            patch.object(session, "commit", side_effect=AssertionError("unexpected commit")),
        ):
            response = client.get(f"/api/v1/queues/{queue_id}/risk-assessment")
        assert response.status_code == 200
        assert response.json()["queue_name"] == "Risk assessment queue"
    finally:
        event.remove(connection, "before_cursor_execute", record_sql)
        session.expire(risk_queue)
    assert len(statements) == 2
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert "count(" in statements[1].lower()
    assert session.execute(select(Ticket.__table__)).all() == before


def test_clock_called_once_and_normalized_to_utc(
    client,
    session,
    risk_queue,
    evaluation_time,
    monkeypatch,
):
    calls = []

    def clock():
        calls.append(True)
        return evaluation_time.astimezone(timezone(timedelta(hours=5, minutes=30)))

    monkeypatch.setitem(app.dependency_overrides, get_evaluation_time, clock)
    body = assess(client, session, risk_queue)
    assert calls == [True]
    assert body["evaluated_at"] == "2026-10-31T16:00:00.123456Z"


def test_aware_timestamps_across_dst(client, session, risk_queue, monkeypatch):
    # Fall-back repeats 01:00 in New York. SLA duration is elapsed time in UTC.
    evaluated = datetime(2026, 11, 1, 1, 15, tzinfo=ZoneInfo("America/New_York"), fold=1)
    created = datetime(2026, 11, 1, 1, 45, tzinfo=ZoneInfo("America/New_York"), fold=0)
    monkeypatch.setitem(app.dependency_overrides, get_evaluation_time, lambda: evaluated)
    agent = add_agent(session, risk_queue)
    # SQLite strips offsets; fixtures must persist UTC there. PostgreSQL verifies offsets directly.
    stored = (
        created
        if session.get_bind().dialect.name == "postgresql"
        else created.astimezone(timezone.utc)
    )
    add_ticket(session, risk_queue, stored, agent=agent)
    body = assess(client, session, risk_queue)
    assert body["evaluated_at"] == "2026-11-01T06:15:00Z"
    assert body["metrics"]["sla_at_risk_tickets"] == 1
    assert body["metrics"]["sla_breached_tickets"] == 0


def test_naive_clock_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        normalize_evaluation_time(datetime(2026, 1, 1))


def test_unrepresentable_cutoffs_rejected(session, risk_queue):
    with pytest.raises(ValueError, match="datetime range"):
        collect_queue_risk_metrics(
            session, risk_queue.id, datetime.min.replace(tzinfo=timezone.utc), RiskPolicy()
        )


@pytest.mark.parametrize("field", list(RiskAssessmentMetrics.model_fields))
def test_metrics_schema_rejects_negative_counts(field):
    values = dict.fromkeys(RiskAssessmentMetrics.model_fields, 0)
    values[field] = -1
    with pytest.raises(ValidationError):
        RiskAssessmentMetrics(**values)
