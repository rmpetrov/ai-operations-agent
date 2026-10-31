from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ai_operations_agent.clock import get_evaluation_time, normalize_evaluation_time
from ai_operations_agent.config import get_risk_policy
from ai_operations_agent.db import get_session
from ai_operations_agent.metrics import collect_queue_risk_metrics
from ai_operations_agent.models import Agent, Ticket
from ai_operations_agent.models import Queue as SupportQueue
from ai_operations_agent.risk import RiskPolicy, evaluate_risk
from ai_operations_agent.schemas import (
    AgentRead,
    AgentStatus,
    QueueMetrics,
    QueueRead,
    QueueRiskAssessment,
    QueueTicketPriorityBreakdown,
    RiskAssessmentMetrics,
    RiskAssessmentPolicy,
    RiskAssessmentSignal,
    TicketCreate,
    TicketPriority,
    TicketPriorityCounts,
    TicketRead,
    TicketsPage,
    TicketStatus,
    TicketUpdateStatus,
)

app = FastAPI(title="AI Operations Agent", version="0.1.0")
DB = Annotated[Session, Depends(get_session)]


@app.get("/health/live")
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness(db: DB) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ready"}


@app.get("/api/v1/queues", response_model=list[QueueRead])
def list_queues(db: DB) -> list[SupportQueue]:
    return list(db.scalars(select(SupportQueue).order_by(SupportQueue.id)).all())


@app.get("/api/v1/agents", response_model=list[AgentRead])
def list_agents(
    db: DB, queue_id: int | None = Query(default=None, gt=0),
    status_filter: AgentStatus | None = Query(default=None, alias="status"),
) -> list[Agent]:
    stmt = select(Agent)
    if queue_id is not None:
        stmt = stmt.where(Agent.queue_id == queue_id)
    if status_filter is not None:
        stmt = stmt.where(Agent.status == status_filter)
    return list(db.scalars(stmt.order_by(Agent.id)).all())


@app.get("/api/v1/tickets", response_model=TicketsPage)
def list_tickets(
    db: DB,
    queue_id: int | None = Query(default=None, gt=0),
    status_filter: TicketStatus | None = Query(default=None, alias="status"),
    priority: TicketPriority | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> TicketsPage:
    filters = []
    if queue_id is not None:
        filters.append(Ticket.queue_id == queue_id)
    if status_filter is not None:
        filters.append(Ticket.status == status_filter)
    if priority is not None:
        filters.append(Ticket.priority == priority)
    total = db.scalar(select(func.count()).select_from(Ticket).where(*filters)) or 0
    stmt = (
        select(Ticket)
        .where(*filters)
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .limit(limit)
        .offset(offset)
    )
    items = list(db.scalars(stmt).all())
    return TicketsPage(items=[TicketRead.model_validate(item) for item in items],
                       total=total, limit=limit, offset=offset)


@app.post("/api/v1/tickets", status_code=status.HTTP_201_CREATED, response_model=TicketRead)
def create_ticket(payload: TicketCreate, db: DB) -> Ticket:
    if db.get(SupportQueue, payload.queue_id) is None:
        raise HTTPException(status_code=404, detail="queue not found")
    if payload.assigned_agent_id is not None:
        agent = db.get(Agent, payload.assigned_agent_id)
        if agent is None or agent.queue_id != payload.queue_id:
            raise HTTPException(status_code=422, detail="assigned agent must belong to queue")
    ticket = Ticket(**payload.model_dump(), status="open")
    db.add(ticket)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Only report a duplicate when we can verify that is the violated invariant.
        existing_id = db.scalar(select(Ticket.id).where(Ticket.external_id == payload.external_id))
        if existing_id is not None:
            raise HTTPException(status_code=409, detail="ticket external_id already exists") from exc
        raise
    db.refresh(ticket)
    return ticket


@app.patch("/api/v1/tickets/{ticket_id}/status", response_model=TicketRead)
def update_ticket_status(ticket_id: int, payload: TicketUpdateStatus, db: DB) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    allowed = {"open": {"in_progress", "resolved"},
               "in_progress": {"resolved"}, "resolved": set()}
    if payload.status not in allowed[ticket.status]:
        raise HTTPException(status_code=409, detail="invalid status transition")
    ticket.status = payload.status
    if payload.status == "resolved":
        ticket.resolved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(ticket)
    return ticket


@app.get(
    "/api/v1/queues/{queue_id}/tickets-by-priority",
    response_model=QueueTicketPriorityBreakdown,
)
def tickets_by_priority(queue_id: int, db: DB) -> QueueTicketPriorityBreakdown:
    if db.get(SupportQueue, queue_id) is None:
        raise HTTPException(status_code=404, detail="queue not found")
    stmt = (
        select(Ticket.priority, func.count(Ticket.id))
        .where(Ticket.queue_id == queue_id, Ticket.status.in_(["open", "in_progress"]))
        .group_by(Ticket.priority)
    )
    counts = {"low": 0, "normal": 0, "high": 0}
    for priority, count in db.execute(stmt):
        counts[priority] = count
    return QueueTicketPriorityBreakdown(
        queue_id=queue_id, counts=TicketPriorityCounts(**counts)
    )


@app.get("/api/v1/queues/{queue_id}/metrics", response_model=QueueMetrics)
def queue_metrics(queue_id: int, db: DB) -> QueueMetrics:
    queue = db.get(SupportQueue, queue_id)
    if queue is None:
        raise HTTPException(status_code=404, detail="queue not found")
    now = datetime.now(timezone.utc)
    active = (Ticket.status.in_(["open", "in_progress"]), Ticket.queue_id == queue_id)
    active_tickets = db.scalar(select(func.count()).select_from(Ticket).where(*active)) or 0
    unassigned_tickets = db.scalar(
        select(func.count()).select_from(Ticket).where(*active, Ticket.assigned_agent_id.is_(None))
    ) or 0
    breached_tickets = db.scalar(
        select(func.count()).select_from(Ticket).where(
            *active, Ticket.created_at < now - timedelta(minutes=queue.sla_minutes)
        )
    ) or 0
    online_agents = db.scalar(
        select(func.count()).select_from(Agent).where(
            Agent.queue_id == queue_id, Agent.status == "online"
        )
    ) or 0
    return QueueMetrics(
        queue_id=queue.id, queue_name=queue.name, active_tickets=active_tickets,
        unassigned_tickets=unassigned_tickets, sla_breached_tickets=breached_tickets,
        online_agents=online_agents, calculated_at=now,
    )


@app.get("/api/v1/queues/{queue_id}/risk-assessment", response_model=QueueRiskAssessment)
def queue_risk_assessment(
    queue_id: int,
    db: DB,
    evaluated_at: Annotated[datetime, Depends(get_evaluation_time)],
    policy: Annotated[RiskPolicy, Depends(get_risk_policy)],
) -> QueueRiskAssessment:
    evaluated_at = normalize_evaluation_time(evaluated_at)
    try:
        facts = collect_queue_risk_metrics(db, queue_id, evaluated_at, policy)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    if facts is None:
        raise HTTPException(status_code=404, detail="queue not found")
    evaluation = evaluate_risk(facts.metrics, policy, facts.warning_window_seconds)
    return QueueRiskAssessment(
        queue_id=facts.queue_id,
        queue_name=facts.queue_name,
        evaluated_at=evaluated_at,
        sla_minutes=facts.sla_minutes,
        policy=RiskAssessmentPolicy(
            version="queue-risk-v1",
            sla_at_risk_remaining_percent=policy.sla_remaining_percent,
            sla_at_risk_window_seconds=facts.warning_window_seconds,
            unassigned_backlog_min_tickets=policy.unassigned_min_tickets,
        ),
        metrics=RiskAssessmentMetrics.model_validate(facts.metrics),
        overall_severity=evaluation.overall_severity,
        signals=[RiskAssessmentSignal.model_validate(signal) for signal in evaluation.signals],
    )
