import pytest
from conftest import seeded_session
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from ai_operations_agent.db import get_session
from ai_operations_agent.main import app
from ai_operations_agent.models import Agent, Queue, Ticket
from ai_operations_agent.tool_schemas import AccessContext
from ai_operations_agent.tools import ToolError, execute_tool


def row_counts(engine):
    with engine.connect() as observer:
        return tuple(observer.scalar(select(func.count()).select_from(model))
                     for model in (Queue, Agent, Ticket))


def test_postgresql_api_commits_and_status_transitions_are_isolated(engine, monkeypatch):
    if engine.dialect.name != "postgresql":
        pytest.skip("checks PostgreSQL outer transactions and savepoints")

    baseline = row_counts(engine)
    # Reuse fixture names and external IDs to catch leakage after full fixture teardown.
    for _ in range(2):
        with seeded_session(engine) as session:
            monkeypatch.setitem(app.dependency_overrides, get_session, lambda: session)
            with TestClient(app) as client:
                queue = session.scalar(select(Queue).where(Queue.name == "Fixture Billing"))
                ticket = session.scalar(select(Ticket).where(Ticket.external_id == "TEST-001"))
                assert ticket.status == "open"
                assert ticket.resolved_at is None

                outer = session.connection().get_transaction()
                access = AccessContext(allowed_queue_ids=frozenset({queue.id}),
                                       allowed_tool_names=frozenset({"list_queues", "get_queue_recommendations"}))
                # A tool's independent read-only transaction cannot observe uncommitted
                # fixtures or reconfigure/roll back the fixture's active write transaction.
                assert execute_tool("list_queues", {}, access, engine=engine) == []
                with pytest.raises(ToolError, match="^queue unavailable$"):
                    execute_tool("get_queue_recommendations", {"queue_id": queue.id}, access, engine=engine)
                assert outer.is_active
                assert session.scalar(select(Queue.id).where(Queue.id == queue.id)) == queue.id

                payload = {"external_id": "ISOLATION-CHECK", "subject": "Check isolation",
                           "queue_id": queue.id, "priority": "high"}
                assert client.post("/api/v1/tickets", json=payload).status_code == 201
                # The API's IntegrityError rollback must preserve earlier fixture/API commits.
                assert client.post("/api/v1/tickets", json=payload).status_code == 409
                endpoint = f"/api/v1/tickets/{ticket.id}/status"
                for status in ("in_progress", "resolved"):
                    response = client.patch(endpoint, json={"status": status})
                    assert response.status_code == 200
                    assert response.json()["status"] == status
                assert response.json()["resolved_at"] is not None
                assert client.patch(endpoint, json={"status": "open"}).status_code == 409
                counts = client.get(f"/api/v1/queues/{queue.id}/tickets-by-priority").json()
                assert counts == {"queue_id": queue.id,
                                  "counts": {"low": 0, "normal": 1, "high": 1}}
                # A separate connection cannot see any of the committed fixture/API changes.
                assert row_counts(engine) == baseline
        # Closing the fixture rolls back inserts and status changes, allowing another run.
        assert row_counts(engine) == baseline
