from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
