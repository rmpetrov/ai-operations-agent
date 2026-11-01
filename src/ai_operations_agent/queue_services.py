"""HTTP-independent queue reads and canonical assessment/response construction."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_operations_agent.assessment import QueueAssessmentContext
from ai_operations_agent.assessment import assess_queue as assess_queue
from ai_operations_agent.models import Queue
from ai_operations_agent.recommendations import build_recommendations
from ai_operations_agent.schemas import (
    QueueRead,
    QueueRecommendations,
    QueueRiskAssessment,
    RiskAssessmentMetrics,
    RiskAssessmentPolicy,
    RiskAssessmentSignal,
)


def list_queues(
    db: Session,
    *,
    allowed_queue_ids: frozenset[int] | None = None,
    selected_queue_id: int | None = None,
) -> list[QueueRead]:
    # None is the existing unscoped local HTTP API. Empty tool scope matches no rows.
    stmt = select(Queue.id, Queue.name, Queue.sla_minutes).order_by(Queue.id)
    if allowed_queue_ids is not None:
        stmt = stmt.where(Queue.id.in_(sorted(allowed_queue_ids)))
    if selected_queue_id is not None:
        stmt = stmt.where(Queue.id == selected_queue_id)
    with db.no_autoflush:
        return [QueueRead.model_validate(row) for row in db.execute(stmt)]


def risk_assessment_response(context: QueueAssessmentContext) -> QueueRiskAssessment:
    facts, policy, evaluation = context.facts, context.policy, context.evaluation
    return QueueRiskAssessment(
        queue_id=facts.queue_id,
        queue_name=facts.queue_name,
        evaluated_at=context.evaluated_at,
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


def recommendations_response(context: QueueAssessmentContext) -> QueueRecommendations:
    return QueueRecommendations(
        assessment=risk_assessment_response(context),
        recommendations=list(build_recommendations(
            context.facts.metrics, context.policy, context.facts.warning_window_seconds,
            context.evaluation,
        )),
    )
