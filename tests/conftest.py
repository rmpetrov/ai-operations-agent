import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ai_operations_agent.clock import get_evaluation_time
from ai_operations_agent.config import settings
from ai_operations_agent.db import get_session
from ai_operations_agent.main import app
from ai_operations_agent.models import Agent, Base, Queue, Ticket


def validate_test_database_url(value: str, development_url: str) -> URL:
    """Reject unsafe targets before SQLAlchemy can open a connection."""
    try:
        url = make_url(value)
    except (ArgumentError, ValueError):
        raise ValueError("TEST_DATABASE_URL must be a valid PostgreSQL URL") from None
    if url.get_backend_name() != "postgresql":
        raise ValueError("TEST_DATABASE_URL must use PostgreSQL; unset it for in-memory SQLite")
    # libpq query arguments can override the database/host encoded in the URL.
    if url.query:
        raise ValueError("TEST_DATABASE_URL must not contain connection query parameters")
    development = make_url(development_url)
    if url.database and url.database.casefold() == (development.database or "").casefold():
        raise ValueError("TEST_DATABASE_URL must not target the configured development database")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*_test", url.database or ""):
        raise ValueError("TEST_DATABASE_URL must name a dedicated database ending in '_test'")
    if not url.host:
        raise ValueError("TEST_DATABASE_URL must specify an explicit host")
    return url


def pytest_configure(config: pytest.Config) -> None:
    value = os.environ.get("TEST_DATABASE_URL")
    if value is not None:
        try:
            validate_test_database_url(value, settings.database_url)
        except ValueError as exc:
            raise pytest.UsageError(str(exc)) from None


@pytest.fixture(scope="session")
def engine() -> Iterator:
    value = os.environ.get("TEST_DATABASE_URL")
    if value is not None:
        url = validate_test_database_url(value, settings.database_url)
        # Run Alembic migrations first; never drop shared PostgreSQL tables in tests.
        engine = create_engine(url, pool_pre_ping=True)
    else:
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        if value is None:
            Base.metadata.drop_all(engine)
        engine.dispose()


@contextmanager
def seeded_session(engine) -> Iterator[Session]:
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
    try:
        seed_session(session)
        yield session
    finally:
        session.close()
        if outer_transaction is not None:
            outer_transaction.rollback()
        if connection is not None:
            connection.close()


def seed_session(session: Session) -> None:
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


@pytest.fixture
def session(engine) -> Iterator[Session]:
    with seeded_session(engine) as session:
        yield session


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = lambda: session
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def evaluation_time(monkeypatch) -> datetime:
    now = datetime(2026, 10, 31, 16, 0, 0, 123456, tzinfo=timezone.utc)
    monkeypatch.setitem(app.dependency_overrides, get_evaluation_time, lambda: now)
    return now
