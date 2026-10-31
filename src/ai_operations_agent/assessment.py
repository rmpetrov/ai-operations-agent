"""Shared read-only queue assessment orchestration, independent of HTTP responses."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from ai_operations_agent.clock import normalize_evaluation_time
from ai_operations_agent.metrics import QueueRiskFacts, collect_queue_risk_metrics
from ai_operations_agent.risk import RiskEvaluation, RiskPolicy, evaluate_risk


@dataclass(frozen=True)
class QueueAssessmentContext:
    evaluated_at: datetime
    policy: RiskPolicy
    facts: QueueRiskFacts
    evaluation: RiskEvaluation


def assess_queue(
    db: Session, queue_id: int, evaluated_at: datetime, policy: RiskPolicy
) -> QueueAssessmentContext | None:
    evaluated_at = normalize_evaluation_time(evaluated_at)
    facts = collect_queue_risk_metrics(db, queue_id, evaluated_at, policy)
    if facts is None:
        return None
    evaluation = evaluate_risk(facts.metrics, policy, facts.warning_window_seconds)
    return QueueAssessmentContext(evaluated_at, policy, facts, evaluation)
