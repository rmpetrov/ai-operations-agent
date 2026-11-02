"""Synthetic fixtures owned by the harness; the runner can only create in-memory SQLite."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ai_operations_agent.models import Agent, Base, Queue, Ticket

EVALUATED_AT = datetime(2026, 10, 31, 16, tzinfo=timezone.utc)


def business_snapshot(engine):
    with engine.connect() as connection:
        return tuple(tuple(connection.execute(select(table).order_by(table.c.id)).all())
                     for table in (Queue.__table__, Agent.__table__, Ticket.__table__))


@dataclass(frozen=True)
class SyntheticFixtures:
    engine: object
    queue_ids: dict[str, int]
    queue_names: dict[str, str]


@contextmanager
def owned_fixtures(engine, *, namespace=None):
    """For the private SQLite engine or the existing *validated* PostgreSQL pytest fixture.

    This helper never creates a PostgreSQL engine/schema, reads a URL, resets existing rows,
    or changes Docker volumes. Only its own committed IDs are deleted, in FK order.
    """
    before = business_snapshot(engine)
    prefix = namespace or f"eval-{uuid4().hex[:12]}"
    queue_ids, queue_names = {}, {}
    try:
        with Session(engine) as seed:
            for key in ("empty", "healthy", "overdue", "upcoming", "unassigned", "offline",
                        "all", "ambiguous_a", "ambiguous_b", "hidden"):
                suffix = {"ambiguous_a": "Twin", "ambiguous_b": "tWIN"}.get(key, key)
                queue = Queue(name=f"{prefix} {suffix}", sla_minutes=30)
                seed.add(queue)
                seed.flush()
                queue_ids[key], queue_names[key] = queue.id, queue.name
                agent = Agent(name="Synthetic operator", queue_id=queue.id,
                              status="offline" if key in {"offline", "all"} else "online")
                seed.add(agent)
                seed.flush()
                ages = {"healthy": (5,), "overdue": (31,), "upcoming": (25,),
                        "unassigned": (5,), "offline": (5,), "all": (31, 25)}.get(key, ())
                for index, age in enumerate(ages):
                    seed.add(Ticket(
                        external_id=f"{prefix}-{key}-{index}", subject="Synthetic ticket",
                        queue_id=queue.id,
                        assigned_agent_id=None if key in {"unassigned", "all"} else agent.id,
                        priority="normal", status="open",
                        created_at=EVALUATED_AT - timedelta(minutes=age),
                    ))
            seed.commit()
        yield SyntheticFixtures(engine, queue_ids, queue_names)
    finally:
        if queue_ids:
            with engine.begin() as cleanup:
                ids = tuple(queue_ids.values())
                cleanup.execute(delete(Ticket).where(Ticket.queue_id.in_(ids)))
                cleanup.execute(delete(Agent).where(Agent.queue_id.in_(ids)))
                cleanup.execute(delete(Queue).where(Queue.id.in_(ids)))
        if business_snapshot(engine) != before:
            raise RuntimeError("synthetic fixture isolation failed")


@contextmanager
def synthetic_database():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    try:
        # Test-only, ephemeral schema. Application startup and migrations remain unchanged.
        Base.metadata.create_all(engine)
        with owned_fixtures(engine, namespace="eval-v1") as fixtures:
            yield fixtures
    finally:
        engine.dispose()
