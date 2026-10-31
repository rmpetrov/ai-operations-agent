from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ai_operations_agent.risk import Severity, SignalCode, SignalSeverity

TicketStatus = Literal["open", "in_progress", "resolved"]
TicketPriority = Literal["low", "normal", "high"]
AgentStatus = Literal["online", "offline"]


class QueueRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    sla_minutes: int


class AgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    queue_id: int
    status: AgentStatus


class TicketRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    external_id: str
    subject: str
    queue_id: int
    assigned_agent_id: int | None
    priority: TicketPriority
    status: TicketStatus
    created_at: datetime
    resolved_at: datetime | None


class TicketCreate(BaseModel):
    external_id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    subject: str = Field(min_length=1, max_length=160)
    queue_id: int = Field(gt=0)
    assigned_agent_id: int | None = Field(default=None, gt=0)
    priority: TicketPriority = "normal"


class TicketUpdateStatus(BaseModel):
    status: TicketStatus


class TicketsPage(BaseModel):
    items: list[TicketRead]
    total: int
    limit: int
    offset: int


class QueueMetrics(BaseModel):
    queue_id: int
    queue_name: str
    active_tickets: int
    unassigned_tickets: int
    sla_breached_tickets: int
    online_agents: int
    calculated_at: datetime


class TicketPriorityCounts(BaseModel):
    low: int = Field(ge=0)
    normal: int = Field(ge=0)
    high: int = Field(ge=0)


class QueueTicketPriorityBreakdown(BaseModel):
    queue_id: int
    counts: TicketPriorityCounts


class RiskAssessmentMetrics(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    active_tickets: int = Field(ge=0)
    unassigned_tickets: int = Field(ge=0)
    sla_breached_tickets: int = Field(ge=0)
    sla_at_risk_tickets: int = Field(ge=0)
    online_agents: int = Field(ge=0)
    future_created_active_tickets: int = Field(ge=0)


class RiskAssessmentPolicy(BaseModel):
    version: Literal["queue-risk-v1"]
    sla_at_risk_remaining_percent: int = Field(ge=1, le=99)
    sla_at_risk_window_seconds: float = Field(gt=0, allow_inf_nan=False)
    unassigned_backlog_min_tickets: int = Field(ge=1)


class RiskAssessmentSignal(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: SignalCode
    severity: SignalSeverity
    affected_tickets: int = Field(gt=0)
    reason: str


class QueueRiskAssessment(BaseModel):
    queue_id: int
    queue_name: str
    evaluated_at: AwareDatetime
    sla_minutes: int = Field(gt=0)
    policy: RiskAssessmentPolicy
    metrics: RiskAssessmentMetrics
    overall_severity: Severity
    signals: list[RiskAssessmentSignal]
