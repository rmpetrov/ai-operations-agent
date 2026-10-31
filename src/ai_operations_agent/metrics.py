"""Read-only SQL aggregation for queue risk assessment."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_operations_agent.models import Agent, Queue, Ticket
from ai_operations_agent.risk import RiskMetrics, RiskPolicy


@dataclass(frozen=True)
class QueueRiskFacts:
    queue_id: int
    queue_name: str
    sla_minutes: int
    warning_window_seconds: float
    metrics: RiskMetrics


def collect_queue_risk_metrics(
    db: Session,
    queue_id: int,
    evaluated_at: datetime,
    policy: RiskPolicy,
) -> QueueRiskFacts | None:
    # Suppress ORM autoflush as well as explicit writes on this read-only path.
    with db.no_autoflush:
        queue = db.execute(
            select(Queue.id, Queue.name, Queue.sla_minutes).where(Queue.id == queue_id)
        ).one_or_none()
        if queue is None:
            return None
        window = policy.warning_window(queue.sla_minutes)
        try:
            due_now_created_at = evaluated_at - timedelta(minutes=queue.sla_minutes)
            warning_created_at = due_now_created_at + window
        except OverflowError as exc:
            raise ValueError("SLA cutoffs exceed the supported datetime range") from exc
        # Compare created_at to bound UTC cutoffs, avoiding dialect-specific date arithmetic.
        online_agents = (
            select(func.count(Agent.id))
            .where(Agent.queue_id == queue_id, Agent.status == "online")
            .scalar_subquery()
        )
        stmt = select(
            func.count(Ticket.id).label("active_tickets"),
            func.count(Ticket.id)
            .filter(Ticket.assigned_agent_id.is_(None))
            .label("unassigned_tickets"),
            func.count(Ticket.id)
            .filter(
                Ticket.created_at <= evaluated_at,
                Ticket.created_at < due_now_created_at,
            )
            .label("sla_breached_tickets"),
            func.count(Ticket.id)
            .filter(
                Ticket.created_at <= evaluated_at,
                Ticket.created_at >= due_now_created_at,
                Ticket.created_at <= warning_created_at,
            )
            .label("sla_at_risk_tickets"),
            online_agents.label("online_agents"),
            func.count(Ticket.id)
            .filter(Ticket.created_at > evaluated_at)
            .label("future_created_active_tickets"),
        ).where(Ticket.queue_id == queue_id, Ticket.status.in_(["open", "in_progress"]))
        counts = db.execute(stmt).mappings().one()
    return QueueRiskFacts(
        queue.id,
        queue.name,
        queue.sla_minutes,
        window.total_seconds(),
        RiskMetrics(**counts),
    )
