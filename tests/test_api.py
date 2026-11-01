from sqlalchemy import select

from ai_operations_agent.models import Agent, Queue, Ticket


def get_ids(session):
    billing = session.scalar(select(Queue).where(Queue.name == "Fixture Billing"))
    technical = session.scalar(select(Queue).where(Queue.name == "Fixture Technical"))
    alex = session.scalar(select(Agent).where(Agent.name == "Alex"))
    return billing.id, technical.id, alex.id


def test_health(client):
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ready"}


def test_list_queues_and_agents(client, session):
    billing_id, _, _ = get_ids(session)
    names = [queue["name"] for queue in client.get("/api/v1/queues").json()]
    assert "Fixture Billing" in names
    agents = client.get("/api/v1/agents", params={"queue_id": billing_id, "status": "online"})
    assert agents.status_code == 200
    assert [agent["name"] for agent in agents.json()] == ["Alex"]


def test_metrics_exclude_resolved(client, session):
    billing_id, _, _ = get_ids(session)
    response = client.get(f"/api/v1/queues/{billing_id}/metrics")
    assert response.status_code == 200
    metrics = response.json()
    assert metrics["active_tickets"] == 2
    assert metrics["unassigned_tickets"] == 1
    assert metrics["sla_breached_tickets"] == 1
    assert metrics["online_agents"] == 1
    assert metrics["queue_name"] == "Fixture Billing"


def test_filtered_ticket_pagination(client, session):
    billing_id, _, _ = get_ids(session)
    response = client.get("/api/v1/tickets", params={
        "queue_id": billing_id, "status": "open", "limit": 1, "offset": 0,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["external_id"] == "TEST-001"


def test_ticket_input_validation(client):
    response = client.post("/api/v1/tickets", json={
        "external_id": "has spaces", "subject": "Help", "queue_id": 1,
    })
    assert response.status_code == 422
    assert client.get("/api/v1/tickets", params={"limit": 101}).status_code == 422
    assert client.get("/api/v1/tickets", params={"status": "unknown"}).status_code == 422


def test_create_duplicate_and_bad_assignment(client, session):
    billing_id, technical_id, alex_id = get_ids(session)
    payload = {"external_id": "TEST-NEW", "subject": "Need assistance",
               "queue_id": billing_id, "priority": "high", "assigned_agent_id": alex_id}
    created = client.post("/api/v1/tickets", json=payload)
    assert created.status_code == 201
    assert created.json()["status"] == "open"
    assert client.post("/api/v1/tickets", json=payload).status_code == 409
    assert client.post("/api/v1/tickets", json={**payload, "external_id": "TEST-OTHER",
               "queue_id": technical_id}).status_code == 422
    assert client.post("/api/v1/tickets", json={**payload, "external_id": "TEST-MISSING",
               "queue_id": 999_999}).status_code == 404


def test_transition_to_resolved_and_block_reopening(client, session):
    ticket = session.scalar(select(Ticket).where(Ticket.external_id == "TEST-001"))
    endpoint = f"/api/v1/tickets/{ticket.id}/status"
    assert client.patch(endpoint, json={"status": "in_progress"}).status_code == 200
    response = client.patch(endpoint, json={"status": "resolved"})
    assert response.status_code == 200
    assert response.json()["resolved_at"] is not None
    assert client.patch(endpoint, json={"status": "open"}).status_code == 409


def test_missing_resources(client):
    assert client.get("/api/v1/queues/999999/metrics").status_code == 404
    assert client.patch("/api/v1/tickets/999999/status", json={"status": "resolved"}).status_code == 404


def test_metrics_after_resolving_breached_ticket(client, session):
    billing_id, _, _ = get_ids(session)
    old_ticket = session.scalar(select(Ticket).where(Ticket.external_id == "TEST-001"))
    assert client.patch(f"/api/v1/tickets/{old_ticket.id}/status",
                        json={"status": "resolved"}).status_code == 200
    metrics = client.get(f"/api/v1/queues/{billing_id}/metrics").json()
    assert metrics["active_tickets"] == 1
    assert metrics["sla_breached_tickets"] == 0


def test_seed_skips_existing_queues(session):
    from ai_operations_agent.seed import seed

    assert seed(session) is False


def test_queue_listing_exact_response_contract(client, session):
    expected = [
        {"id": queue.id, "name": queue.name, "sla_minutes": queue.sla_minutes}
        for queue in session.scalars(select(Queue).order_by(Queue.id))
    ]
    response = client.get("/api/v1/queues")
    assert response.status_code == 200
    assert response.json() == expected
