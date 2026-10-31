"""Create support queues, agents, and tickets.

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "queues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("sla_minutes", sa.Integer(), nullable=False),
        sa.CheckConstraint("sla_minutes > 0", name="ck_queues_positive_sla"),
    )
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("queue_id", sa.Integer(), sa.ForeignKey("queues.id"), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.CheckConstraint("status IN ('online', 'offline')", name="ck_agents_status"),
    )
    op.create_index("ix_agents_queue_id", "agents", ["queue_id"])
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("external_id", sa.String(32), nullable=False, unique=True),
        sa.Column("subject", sa.String(160), nullable=False),
        sa.Column("queue_id", sa.Integer(), sa.ForeignKey("queues.id"), nullable=False),
        sa.Column("assigned_agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("priority", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("priority IN ('low', 'normal', 'high')", name="ck_tickets_priority"),
        sa.CheckConstraint("status IN ('open', 'in_progress', 'resolved')", name="ck_tickets_status"),
        sa.CheckConstraint(
            "(status = 'resolved' AND resolved_at IS NOT NULL) OR "
            "(status <> 'resolved' AND resolved_at IS NULL)",
            name="ck_tickets_resolution_state",
        ),
    )
    op.create_index("ix_tickets_queue_status_created", "tickets", ["queue_id", "status", "created_at"])
    op.create_index("ix_tickets_assigned_agent_id", "tickets", ["assigned_agent_id"])


def downgrade() -> None:
    op.drop_index("ix_tickets_assigned_agent_id", table_name="tickets")
    op.drop_index("ix_tickets_queue_status_created", table_name="tickets")
    op.drop_table("tickets")
    op.drop_index("ix_agents_queue_id", table_name="agents")
    op.drop_table("agents")
    op.drop_table("queues")
