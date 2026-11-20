"""Strict, deeply immutable internal triage values; not public or model contracts."""

from collections import Counter
from datetime import timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from ai_operations_agent.risk import (
    RiskEvaluation,
    RiskMetrics,
    RiskPolicy,
    RiskSignal,
    evaluate_risk,
)
from ai_operations_agent.schemas import (
    QueueRiskAssessment,
    RiskAssessmentMetrics,
    RiskAssessmentPolicy,
    RiskAssessmentSignal,
)
from ai_operations_agent.triage_policy import MAX_TRIAGE_QUEUES, operational_key

QueueID = Annotated[int, Field(strict=True, gt=0)]
QueueCount = Annotated[int, Field(strict=True, ge=0, le=MAX_TRIAGE_QUEUES)]
QueueIDs = Annotated[tuple[QueueID, ...], Field(max_length=MAX_TRIAGE_QUEUES)]


class InternalValue(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, revalidate_instances="always",
    )


class TriageScope(InternalValue):
    mode: Literal["authorized_scope", "selected_queue"]
    effective_queue_ids: QueueIDs = Field(min_length=1)
    selected_queue_id: QueueID | None = None

    @model_validator(mode="after")
    def consistent_scope(self):
        ids = self.effective_queue_ids
        if ids != tuple(sorted(set(ids))):
            raise ValueError("scope IDs must be unique and sorted")
        if self.mode == "selected_queue":
            if self.selected_queue_id is None or ids != (self.selected_queue_id,):
                raise ValueError("selected scope must be a singleton")
        elif self.selected_queue_id is not None:
            raise ValueError("unselected scope cannot carry a selection")
        return self


class TriageRiskPolicy(InternalValue):
    version: Literal["queue-risk-v1"] = "queue-risk-v1"
    sla_at_risk_remaining_percent: int = Field(ge=1, le=99)
    unassigned_backlog_min_tickets: int = Field(ge=1)

    def risk_policy(self) -> RiskPolicy:
        return RiskPolicy(self.sla_at_risk_remaining_percent, self.unassigned_backlog_min_tickets)


class TriageMetrics(RiskAssessmentMetrics, InternalValue):
    def risk_metrics(self) -> RiskMetrics:
        return RiskMetrics(**self.model_dump())


class TriageAssessmentPolicy(RiskAssessmentPolicy, InternalValue):
    pass


class TriageSignal(RiskAssessmentSignal, InternalValue):
    pass


class TriageAssessment(QueueRiskAssessment, InternalValue):
    queue_id: QueueID
    queue_name: str = Field(min_length=1, max_length=80)
    policy: TriageAssessmentPolicy
    metrics: TriageMetrics
    signals: tuple[TriageSignal, ...] = Field(max_length=4)

    def risk_evaluation(self) -> RiskEvaluation:
        return RiskEvaluation(
            self.overall_severity,
            tuple(RiskSignal(**signal.model_dump()) for signal in self.signals),
        )

    def operational_key(self) -> tuple[int, int, int, int, int, int]:
        return operational_key(self.metrics.risk_metrics(), self.risk_evaluation())

    @model_validator(mode="after")
    def canonical_assessment(self):
        if self.evaluated_at.utcoffset() != timedelta(0):
            raise ValueError("evaluation time must be UTC")
        m = self.metrics
        if (m.unassigned_tickets > m.active_tickets or
                m.sla_breached_tickets + m.sla_at_risk_tickets + m.future_created_active_tickets
                > m.active_tickets):
            raise ValueError("inconsistent ticket counts")
        policy = RiskPolicy(
            self.policy.sla_at_risk_remaining_percent, self.policy.unassigned_backlog_min_tickets,
        )
        try:
            window = policy.warning_window(self.sla_minutes).total_seconds()
        except OverflowError:
            raise ValueError("unsupported SLA duration") from None
        if self.policy.sla_at_risk_window_seconds != window:
            raise ValueError("inconsistent warning window")
        expected = evaluate_risk(m.risk_metrics(), policy, window)
        if self.risk_evaluation() != expected:
            raise ValueError("assessment must contain the complete canonical risk evaluation")
        return self


class RankedQueue(InternalValue):
    position: int = Field(ge=1, le=MAX_TRIAGE_QUEUES)
    assessment: TriageAssessment
    operational_tie: bool
    detail_eligible: bool
    data_warnings: tuple[Literal["future_created_active_tickets"], ...] = Field(max_length=1)

    @model_validator(mode="after")
    def consistent_classifications(self):
        if self.detail_eligible != bool(self.assessment.signals):
            raise ValueError("detail eligibility requires a triggered signal")
        expected = ("future_created_active_tickets",) \
            if self.assessment.metrics.future_created_active_tickets else ()
        if self.data_warnings != expected:
            raise ValueError("inconsistent data warning")
        return self


class QueueTriageResult(InternalValue):
    evaluated_at: AwareDatetime
    ranking_policy_version: Literal["queue-triage-v1"] = "queue-triage-v1"
    risk_policy: TriageRiskPolicy
    scope: TriageScope
    unavailable_queue_ids: QueueIDs
    available_queue_count: QueueCount
    unavailable_queue_count: QueueCount
    rankings: tuple[RankedQueue, ...] = Field(max_length=MAX_TRIAGE_QUEUES)
    eligible_queue_count: QueueCount
    detail_target_queue_id: QueueID | None
    outcome: Literal["no_available_queues", "no_triggered_rules", "attention_required"]

    @model_validator(mode="after")
    def consistent_result(self):
        if self.evaluated_at.utcoffset() != timedelta(0):
            raise ValueError("evaluation time must be UTC")
        ids = tuple(row.assessment.queue_id for row in self.rankings)
        missing = self.unavailable_queue_ids
        if len(set(ids)) != len(ids) or missing != tuple(sorted(set(missing))):
            raise ValueError("duplicate or unordered queue IDs")
        if set(ids) & set(missing) or set(ids) | set(missing) != set(self.scope.effective_queue_ids):
            raise ValueError("available and unavailable IDs must partition the effective scope")
        if self.scope.mode == "selected_queue" and missing:
            raise ValueError("selected queue unavailable")
        if (self.available_queue_count != len(ids) or
                self.unavailable_queue_count != len(missing)):
            raise ValueError("inconsistent queue counts")
        keys = [row.assessment.operational_key() for row in self.rankings]
        if list(zip(keys, ids, strict=True)) != sorted(zip(keys, ids, strict=True)):
            raise ValueError("incorrect queue ranking")
        ties = Counter(keys)
        eligible = []
        for position, row in enumerate(self.rankings, 1):
            a = row.assessment
            p = a.policy
            if (a.evaluated_at != self.evaluated_at or
                    p.version != self.risk_policy.version or
                    p.sla_at_risk_remaining_percent != self.risk_policy.sla_at_risk_remaining_percent or
                    p.unassigned_backlog_min_tickets != self.risk_policy.unassigned_backlog_min_tickets):
                raise ValueError("mismatched evaluation metadata")
            if row.position != position or row.operational_tie != (ties[a.operational_key()] > 1):
                raise ValueError("incorrect position or operational tie")
            if row.detail_eligible:
                eligible.append(a.queue_id)
        if (self.eligible_queue_count != len(eligible) or
                self.detail_target_queue_id != (eligible[0] if eligible else None)):
            raise ValueError("incorrect eligible count or detail target")
        expected = "attention_required" if eligible else (
            "no_triggered_rules" if ids else "no_available_queues"
        )
        if self.outcome != expected:
            raise ValueError("incorrect triage outcome")
        return self
