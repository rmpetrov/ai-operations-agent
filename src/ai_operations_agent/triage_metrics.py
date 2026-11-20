"""Bounded scalar-only batch reads inside the caller-owned read-only snapshot."""

from datetime import datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ai_operations_agent.metrics import QueueRiskFacts
from ai_operations_agent.models import Agent, Queue, Ticket
from ai_operations_agent.risk import RiskMetrics, RiskPolicy
from ai_operations_agent.triage_schemas import TriageScope


def collect_triage_metrics(
    db: Session, scope: TriageScope, evaluated_at: datetime, policy: RiskPolicy,
) -> tuple[QueueRiskFacts, ...]:
    """One metadata SELECT plus one aggregate SELECT (only metadata when none exist)."""
    with db.no_autoflush:
        queues = db.execute(
            select(Queue.id, Queue.name, Queue.sla_minutes)
            .where(Queue.id.in_(scope.effective_queue_ids)).order_by(Queue.id),
        ).all()
        if not queues:
            return ()
        ids = tuple(queue.id for queue in queues)
        due, warning, windows = {}, {}, {}
        for queue in queues:
            windows[queue.id] = policy.warning_window(queue.sla_minutes)
            # Bound UTC datetimes retain the existing cross-dialect microsecond semantics.
            due[queue.id] = evaluated_at - timedelta(minutes=queue.sla_minutes)
            warning[queue.id] = due[queue.id] + windows[queue.id]
        due_cutoff = case(due, value=Ticket.queue_id)
        warning_cutoff = case(warning, value=Ticket.queue_id)
        tickets = select(
            Ticket.queue_id,
            func.count(Ticket.id).label("active_tickets"),
            func.count(Ticket.id).filter(Ticket.assigned_agent_id.is_(None))
            .label("unassigned_tickets"),
            func.count(Ticket.id).filter(
                Ticket.created_at <= evaluated_at, Ticket.created_at < due_cutoff,
            ).label("sla_breached_tickets"),
            func.count(Ticket.id).filter(
                Ticket.created_at <= evaluated_at, Ticket.created_at >= due_cutoff,
                Ticket.created_at <= warning_cutoff,
            ).label("sla_at_risk_tickets"),
            func.count(Ticket.id).filter(Ticket.created_at > evaluated_at)
            .label("future_created_active_tickets"),
        ).where(
            Ticket.queue_id.in_(ids), Ticket.status.in_(("open", "in_progress")),
        ).group_by(Ticket.queue_id).subquery()
        agents = select(
            Agent.queue_id, func.count(Agent.id).label("online_agents"),
        ).where(Agent.queue_id.in_(ids), Agent.status == "online").group_by(Agent.queue_id).subquery()
        names = (
            "active_tickets", "unassigned_tickets", "sla_breached_tickets",
            "sla_at_risk_tickets", "future_created_active_tickets",
        )
        rows = db.execute(select(
            Queue.id,
            *(func.coalesce(tickets.c[name], 0).label(name) for name in names),
            func.coalesce(agents.c.online_agents, 0).label("online_agents"),
        ).outerjoin(tickets, tickets.c.queue_id == Queue.id)
            .outerjoin(agents, agents.c.queue_id == Queue.id)
            .where(Queue.id.in_(ids)).order_by(Queue.id)).mappings().all()
        if tuple(row["id"] for row in rows) != ids:
            raise ValueError("inconsistent queue snapshot")
        return tuple(QueueRiskFacts(
            queue.id, queue.name, queue.sla_minutes, windows[queue.id].total_seconds(),
            RiskMetrics(**{key: value for key, value in row.items() if key != "id"}),
        ) for queue, row in zip(queues, rows, strict=True))
