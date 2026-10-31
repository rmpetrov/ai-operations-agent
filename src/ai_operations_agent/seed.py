"""Insert a reproducible sample support operation into an empty local database."""

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_operations_agent.db import get_engine
from ai_operations_agent.models import Agent, Queue, Ticket


def seed(session: Session) -> bool:
    if session.scalar(select(Queue.id).limit(1)) is not None:
        return False
    rng = random.Random(20261027)
    queues = [
        Queue(name="Technical Support", sla_minutes=60),
        Queue(name="Billing", sla_minutes=90),
        Queue(name="Account Access", sla_minutes=30),
    ]
    session.add_all(queues)
    session.flush()
    agents = [
        Agent(name=f"{name} {i+1:02d}", queue_id=queue.id,
              status="online" if i < 3 else "offline")
        for queue, name in zip(queues, ["Technical Agent", "Billing Agent", "Access Agent"], strict=True)
        for i in range(4)
    ]
    session.add_all(agents)
    session.flush()
    now = datetime.now(timezone.utc)
    subjects = [
        "Cannot sign in", "Refund request", "API timeout", "Subscription question",
        "Password reset", "Integration error", "Incorrect invoice", "Missing email",
    ]
    tickets = []
    for i in range(1, 91):
        queue = queues[(i - 1) % len(queues)]
        same_queue_agents = [agent for agent in agents if agent.queue_id == queue.id]
        state = rng.choices(["open", "in_progress", "resolved"], [50, 35, 15])[0]
        created_at = now - timedelta(minutes=rng.randint(5, 240))
        tickets.append(Ticket(
            external_id=f"TCK-{i:04d}", subject=subjects[(i - 1) % len(subjects)],
            queue_id=queue.id, assigned_agent_id=(rng.choice(same_queue_agents).id
                                                if rng.random() > 0.35 else None),
            priority=rng.choices(["low", "normal", "high"], [20, 60, 20])[0],
            status=state, created_at=created_at,
            resolved_at=(created_at + timedelta(minutes=5)
                         if state == "resolved" else None),
        ))
    session.add_all(tickets)
    session.commit()
    return True


def main() -> None:
    with Session(get_engine()) as session:
        inserted = seed(session)
    if inserted:
        print("Seeded 3 queues, 12 agents and 90 tickets")
    else:
        print("Skipped: queues already exist (seed is intentionally non-destructive)")


if __name__ == "__main__":
    main()
