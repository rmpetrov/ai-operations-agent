from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Queue(Base):
    __tablename__ = "queues"
    __table_args__ = (CheckConstraint("sla_minutes > 0", name="ck_queues_positive_sla"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    sla_minutes: Mapped[int] = mapped_column(Integer)


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (CheckConstraint("status IN ('online', 'offline')", name="ck_agents_status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    queue_id: Mapped[int] = mapped_column(ForeignKey("queues.id"), index=True)
    status: Mapped[str] = mapped_column(String(12))


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint("priority IN ('low', 'normal', 'high')", name="ck_tickets_priority"),
        CheckConstraint("status IN ('open', 'in_progress', 'resolved')", name="ck_tickets_status"),
        CheckConstraint(
            "(status = 'resolved' AND resolved_at IS NOT NULL) OR "
            "(status <> 'resolved' AND resolved_at IS NULL)",
            name="ck_tickets_resolution_state",
        ),
        Index("ix_tickets_queue_status_created", "queue_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(32), unique=True)
    subject: Mapped[str] = mapped_column(String(160))
    queue_id: Mapped[int] = mapped_column(ForeignKey("queues.id"))
    assigned_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"), index=True)
    priority: Mapped[str] = mapped_column(String(8), default="normal")
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
