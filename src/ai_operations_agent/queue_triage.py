"""Trusted deterministic triage foundation. No agent tool, routing, provider or writes."""

from collections import Counter
from datetime import datetime

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ai_operations_agent.clock import normalize_evaluation_time
from ai_operations_agent.metrics import QueueRiskFacts
from ai_operations_agent.read_only import read_only_session
from ai_operations_agent.risk import RiskPolicy, evaluate_risk
from ai_operations_agent.tool_schemas import AccessContext
from ai_operations_agent.triage_metrics import collect_triage_metrics
from ai_operations_agent.triage_policy import MAX_TRIAGE_QUEUES, RANKING_POLICY_VERSION
from ai_operations_agent.triage_schemas import (
    QueueTriageResult,
    RankedQueue,
    TriageAssessment,
    TriageAssessmentPolicy,
    TriageMetrics,
    TriageRiskPolicy,
    TriageScope,
    TriageSignal,
)


class TriageError(ValueError):
    """Fixed sanitized boundary errors; never include inputs or database details."""


def effective_triage_scope(access: AccessContext) -> TriageScope:
    """Resolve grants before acquiring any connection; tool grants are not used here."""
    if not isinstance(access, AccessContext) or not access.allowed_queue_ids:
        raise TriageError("tool access denied")
    selected = access.selected_queue_id
    if selected is not None and selected not in access.allowed_queue_ids:
        raise TriageError("queue unavailable")
    if selected is None and len(access.allowed_queue_ids) > MAX_TRIAGE_QUEUES:
        raise TriageError("triage scope exceeds limit")
    ids = (selected,) if selected is not None else tuple(sorted(access.allowed_queue_ids))
    return TriageScope(
        mode="selected_queue" if selected is not None else "authorized_scope",
        effective_queue_ids=ids, selected_queue_id=selected,
    )


def build_triage_result(
    facts: tuple[QueueRiskFacts, ...], scope: TriageScope,
    evaluated_at: datetime, policy: RiskPolicy,
) -> QueueTriageResult:
    """Build and validate a complete result from trusted facts, without I/O."""
    assessments = []
    for fact in facts:
        metrics = TriageMetrics.model_validate(fact.metrics, strict=True, from_attributes=True)
        evaluation = evaluate_risk(metrics.risk_metrics(), policy, fact.warning_window_seconds)
        assessments.append(TriageAssessment(
            queue_id=fact.queue_id, queue_name=fact.queue_name, evaluated_at=evaluated_at,
            sla_minutes=fact.sla_minutes,
            policy=TriageAssessmentPolicy(
                version="queue-risk-v1", sla_at_risk_remaining_percent=policy.sla_remaining_percent,
                sla_at_risk_window_seconds=fact.warning_window_seconds,
                unassigned_backlog_min_tickets=policy.unassigned_min_tickets,
            ), metrics=metrics, overall_severity=evaluation.overall_severity,
            signals=tuple(TriageSignal.model_validate(signal, strict=True, from_attributes=True)
                          for signal in evaluation.signals),
        ))
    assessments.sort(key=lambda a: (*a.operational_key(), a.queue_id))
    ties = Counter(a.operational_key() for a in assessments)
    rankings = tuple(RankedQueue(
        position=position, assessment=a,
        operational_tie=ties[a.operational_key()] > 1, detail_eligible=bool(a.signals),
        data_warnings=("future_created_active_tickets",)
        if a.metrics.future_created_active_tickets else (),
    ) for position, a in enumerate(assessments, 1))
    eligible = [row.assessment.queue_id for row in rankings if row.detail_eligible]
    unavailable = tuple(sorted(set(scope.effective_queue_ids) - {a.queue_id for a in assessments}))
    return QueueTriageResult(
        evaluated_at=evaluated_at, ranking_policy_version=RANKING_POLICY_VERSION,
        risk_policy=TriageRiskPolicy(
            sla_at_risk_remaining_percent=policy.sla_remaining_percent,
            unassigned_backlog_min_tickets=policy.unassigned_min_tickets,
        ), scope=scope, unavailable_queue_ids=unavailable,
        available_queue_count=len(rankings), unavailable_queue_count=len(unavailable),
        rankings=rankings, eligible_queue_count=len(eligible),
        detail_target_queue_id=eligible[0] if eligible else None,
        outcome="attention_required" if eligible else (
            "no_triggered_rules" if rankings else "no_available_queues"
        ),
    )


def triage_queues(
    engine: Engine, access: AccessContext, *, evaluated_at: datetime, policy: RiskPolicy,
) -> QueueTriageResult:
    """Explicit trusted inputs only. All sessions close before a result is returned."""
    scope = effective_triage_scope(access)
    if not isinstance(engine, Engine):
        # The shared helper permits None for legacy callers. This operation never
        # falls back to an ambient/development engine or borrows a caller session.
        raise TypeError("triage requires an explicit Engine")
    evaluated_at = normalize_evaluation_time(evaluated_at)
    if type(policy) is not RiskPolicy:
        raise TriageError("invalid triage policy")
    # Revalidate parameters before database access, including forged dataclass instances.
    policy = RiskPolicy(policy.sla_remaining_percent, policy.unassigned_min_tickets)
    try:
        with read_only_session(
            engine, statement_timeout_ms=access.limits.statement_timeout_ms, snapshot=True,
        ) as db:
            facts = collect_triage_metrics(db, scope, evaluated_at, policy)
        if scope.mode == "selected_queue" and not facts:
            raise TriageError("queue unavailable")
        return build_triage_result(facts, scope, evaluated_at, policy)
    except SQLAlchemyError:
        raise TriageError("database unavailable") from None
    except TriageError:
        raise
    except (ValueError, TypeError, OverflowError):
        raise TriageError("invalid triage data") from None
