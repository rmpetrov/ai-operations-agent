from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from ai_operations_agent.models import Queue, Ticket
from ai_operations_agent.schemas import QueueTicketPriorityBreakdown


@pytest.fixture
def priority_queue(session):
    queue = Queue(name="Priority breakdown queue", sla_minutes=30)
    session.add(queue)
    session.commit()
    return queue


def add_priority_tickets(session, queue_id, priority, status, count):
    tickets = [
        Ticket(
            external_id=f"PRIO-{queue_id}-{priority}-{status[0]}-{index}",
            subject="Priority breakdown fixture",
            queue_id=queue_id,
            priority=priority,
            status=status,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            resolved_at=(
                datetime(2026, 1, 2, tzinfo=timezone.utc) if status == "resolved" else None
            ),
        )
        for index in range(count)
    ]
    session.add_all(tickets)
    session.commit()
    return tickets


def test_ticket_priority_breakdown_exact_fixture_contract(client, session):
    billing = session.scalar(select(Queue).where(Queue.name == "Fixture Billing"))
    response = client.get(f"/api/v1/queues/{billing.id}/tickets-by-priority")
    assert response.status_code == 200
    assert response.json() == {
        "queue_id": billing.id,
        "counts": {"low": 0, "normal": 1, "high": 1},
    }


def test_ticket_priority_breakdown_empty_queue(client, priority_queue):
    response = client.get(f"/api/v1/queues/{priority_queue.id}/tickets-by-priority")
    assert response.status_code == 200
    assert response.json() == {
        "queue_id": priority_queue.id,
        "counts": {"low": 0, "normal": 0, "high": 0},
    }


def test_ticket_priority_breakdown_resolved_only(client, session, priority_queue):
    for priority in ("low", "normal", "high"):
        add_priority_tickets(session, priority_queue.id, priority, "resolved", 2)
    response = client.get(f"/api/v1/queues/{priority_queue.id}/tickets-by-priority")
    assert response.status_code == 200
    assert response.json() == {
        "queue_id": priority_queue.id,
        "counts": {"low": 0, "normal": 0, "high": 0},
    }


def test_ticket_priority_breakdown_multiple_tickets_per_priority(client, session, priority_queue):
    expected_counts = {"low": 2, "normal": 3, "high": 4}
    for priority, count in expected_counts.items():
        add_priority_tickets(session, priority_queue.id, priority, "open", count)
    response = client.get(f"/api/v1/queues/{priority_queue.id}/tickets-by-priority")
    assert response.status_code == 200
    assert response.json() == {"queue_id": priority_queue.id, "counts": expected_counts}


@pytest.mark.parametrize("priority", ["low", "normal", "high"])
def test_ticket_priority_breakdown_both_active_statuses(client, session, priority_queue, priority):
    add_priority_tickets(session, priority_queue.id, priority, "open", 1)
    add_priority_tickets(session, priority_queue.id, priority, "in_progress", 1)
    expected_counts = {"low": 0, "normal": 0, "high": 0}
    expected_counts[priority] = 2
    response = client.get(f"/api/v1/queues/{priority_queue.id}/tickets-by-priority")
    assert response.status_code == 200
    assert response.json() == {"queue_id": priority_queue.id, "counts": expected_counts}


@pytest.mark.parametrize(
    ("initial_status", "transitions"),
    [
        ("open", ["in_progress", "resolved"]),
        ("open", ["resolved"]),
        ("in_progress", ["resolved"]),
    ],
)
def test_ticket_priority_breakdown_status_transitions(
    client, session, priority_queue, initial_status, transitions,
):
    ticket = add_priority_tickets(session, priority_queue.id, "high", initial_status, 1)[0]
    add_priority_tickets(session, priority_queue.id, "low", "open", 2)
    endpoint = f"/api/v1/queues/{priority_queue.id}/tickets-by-priority"
    initial = client.get(endpoint)
    assert initial.status_code == 200
    assert initial.json() == {
        "queue_id": priority_queue.id,
        "counts": {"low": 2, "normal": 0, "high": 1},
    }
    for status in transitions:
        changed = client.patch(f"/api/v1/tickets/{ticket.id}/status", json={"status": status})
        assert changed.status_code == 200
        response = client.get(endpoint)
        assert response.status_code == 200
        assert response.json() == {
            "queue_id": priority_queue.id,
            "counts": {"low": 2, "normal": 0, "high": 0 if status == "resolved" else 1},
        }


def test_ticket_priority_breakdown_queue_isolation(client, session, priority_queue):
    billing = session.scalar(select(Queue).where(Queue.name == "Fixture Billing"))
    technical = session.scalar(select(Queue).where(Queue.name == "Fixture Technical"))
    add_priority_tickets(session, priority_queue.id, "low", "open", 3)
    add_priority_tickets(session, priority_queue.id, "normal", "in_progress", 2)
    add_priority_tickets(session, priority_queue.id, "high", "open", 4)
    add_priority_tickets(session, priority_queue.id, "high", "resolved", 3)
    for queue_id, counts in [
        (billing.id, {"low": 0, "normal": 1, "high": 1}),
        (technical.id, {"low": 0, "normal": 1, "high": 0}),
        (priority_queue.id, {"low": 3, "normal": 2, "high": 4}),
    ]:
        response = client.get(f"/api/v1/queues/{queue_id}/tickets-by-priority")
        assert response.status_code == 200
        assert response.json() == {"queue_id": queue_id, "counts": counts}


@pytest.mark.parametrize("queue_id", [999_999, 0, -1])
def test_ticket_priority_breakdown_nonexistent_queue(client, queue_id):
    response = client.get(f"/api/v1/queues/{queue_id}/tickets-by-priority")
    assert response.status_code == 404
    assert response.json() == {"detail": "queue not found"}


@pytest.mark.parametrize("queue_id", ["not-an-integer", "1.5"])
def test_ticket_priority_breakdown_invalid_path_id(client, queue_id):
    response = client.get(f"/api/v1/queues/{queue_id}/tickets-by-priority")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["path", "queue_id"]


@pytest.mark.parametrize("priority", ["low", "normal", "high"])
def test_ticket_priority_breakdown_schema_rejects_negative_counts(priority):
    counts = {"low": 0, "normal": 0, "high": 0}
    counts[priority] = -1
    with pytest.raises(ValidationError) as exc_info:
        QueueTicketPriorityBreakdown(queue_id=1, counts=counts)
    assert exc_info.value.errors()[0]["loc"] == ("counts", priority)
    assert exc_info.value.errors()[0]["type"] == "greater_than_equal"
