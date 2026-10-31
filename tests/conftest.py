import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ai_operations_agent.db import get_session
from ai_operations_agent.main import app
from ai_operations_agent.models import Agent, Base, Queue, Ticket


@pytest.fixture(scope="session")
def engine() -> Iterator:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        # Run Alembic migrations first; never drop shared PostgreSQL tables in tests.
        engine = create_engine(url, pool_pre_ping=True)
    else:
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
    yield engine
    if not url:
        Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def session(engine) -> Iterator[Session]:
    if engine.dialect.name == "sqlite":
        # SQLite's driver has different SAVEPOINT semantics. Reset tables per test.
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        session = Session(engine)
        connection = None
        outer_transaction = None
    else:
        connection = engine.connect()
        outer_transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
    billing = Queue(name="Fixture Billing", sla_minutes=30)
    technical = Queue(name="Fixture Technical", sla_minutes=45)
    session.add_all([billing, technical])
    session.flush()
    online = Agent(name="Alex", queue_id=billing.id, status="online")
    offline = Agent(name="Taylor", queue_id=billing.id, status="offline")
    tech = Agent(name="Sam", queue_id=technical.id, status="online")
    session.add_all([online, offline, tech])
    session.flush()
    now = datetime.now(timezone.utc)
    session.add_all([
        Ticket(external_id="TEST-001", subject="Old billing", queue_id=billing.id,
               status="open", priority="high", created_at=now-timedelta(minutes=100)),
        Ticket(external_id="TEST-002", subject="Active billing", queue_id=billing.id,
               assigned_agent_id=online.id, status="in_progress", priority="normal",
               created_at=now-timedelta(minutes=10)),
        Ticket(external_id="TEST-003", subject="Resolved billing", queue_id=billing.id,
               status="resolved", priority="low", created_at=now-timedelta(minutes=200),
               resolved_at=now-timedelta(minutes=150)),
        Ticket(external_id="TEST-004", subject="New tech", queue_id=technical.id,
               status="open", priority="normal", created_at=now-timedelta(minutes=5)),
    ])
    session.commit()
    yield session
    session.close()
    if outer_transaction is not None:
        outer_transaction.rollback()
    if connection is not None:
        connection.close()


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
