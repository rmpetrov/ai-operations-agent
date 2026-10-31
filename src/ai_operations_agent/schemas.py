from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, model_validator

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


class RecommendationValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OverdueEvidence(RecommendationValue):
    sla_breached_tickets: StrictInt = Field(gt=0)


class UpcomingDeadlineEvidence(RecommendationValue):
    sla_at_risk_tickets: StrictInt = Field(gt=0)
    sla_at_risk_window_seconds: float = Field(gt=0, allow_inf_nan=False)


class RecordedAgentStatusEvidence(RecommendationValue):
    active_tickets: StrictInt = Field(gt=0)
    online_agents: StrictInt = Field(ge=0, le=0)


class UnassignedEvidence(RecommendationValue):
    unassigned_tickets: StrictInt = Field(gt=0)
    unassigned_backlog_min_tickets: StrictInt = Field(ge=1)


class RecommendationBase(RecommendationValue):
    suggested_action: str = Field(min_length=1)
    explanation: str = Field(min_length=1)


class ReviewOverdueTickets(RecommendationBase):
    code: Literal["REVIEW_OVERDUE_TICKETS"] = "REVIEW_OVERDUE_TICKETS"
    priority: Literal["high"] = "high"
    triggered_by_signals: tuple[Literal["SLA_BREACHED"]] = ("SLA_BREACHED",)
    evidence: OverdueEvidence


class ReviewUpcomingDeadlines(RecommendationBase):
    code: Literal["REVIEW_UPCOMING_DEADLINES"] = "REVIEW_UPCOMING_DEADLINES"
    priority: Literal["medium"] = "medium"
    triggered_by_signals: tuple[Literal["SLA_AT_RISK"]] = ("SLA_AT_RISK",)
    evidence: UpcomingDeadlineEvidence


class VerifyRecordedAgentStatus(RecommendationBase):
    code: Literal["VERIFY_RECORDED_AGENT_STATUS"] = "VERIFY_RECORDED_AGENT_STATUS"
    priority: Literal["medium"] = "medium"
    triggered_by_signals: tuple[Literal["NO_AGENTS_ONLINE"]] = ("NO_AGENTS_ONLINE",)
    evidence: RecordedAgentStatusEvidence


class TriageUnassignedTickets(RecommendationBase):
    code: Literal["TRIAGE_UNASSIGNED_TICKETS"] = "TRIAGE_UNASSIGNED_TICKETS"
    priority: Literal["low"] = "low"
    triggered_by_signals: tuple[Literal["UNASSIGNED_BACKLOG"]] = ("UNASSIGNED_BACKLOG",)
    evidence: UnassignedEvidence


ActionRecommendation = Annotated[
    ReviewOverdueTickets | ReviewUpcomingDeadlines | VerifyRecordedAgentStatus
    | TriageUnassignedTickets,
    Field(discriminator="code"),
]


class QueueRecommendations(RecommendationValue):
    recommendation_policy_version: Literal["queue-recommendations-v1"] = "queue-recommendations-v1"
    advisory_only: Literal[True] = True
    requires_human_review: Literal[True] = True
    assessment: QueueRiskAssessment
    recommendations: list[ActionRecommendation] = Field(max_length=4)

    @model_validator(mode="after")
    def validate_traceability(self):
        signals = {signal.code for signal in self.assessment.signals}
        codes = [item.code for item in self.recommendations]
        if len(codes) != len(set(codes)):
            raise ValueError("recommendation codes must be unique")
        for item in self.recommendations:
            if not set(item.triggered_by_signals) <= signals:
                raise ValueError("recommendation trigger must appear in assessment signals")
            for name, value in item.evidence.model_dump().items():
                source = (
                    self.assessment.metrics
                    if name in RiskAssessmentMetrics.model_fields
                    else self.assessment.policy
                )
                if value != getattr(source, name):
                    raise ValueError(f"recommendation evidence {name} must match assessment")
        return self
