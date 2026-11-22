"""Bounded offline-agent-v2 contracts. No model turn or runtime integration."""

import json
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)

from ai_operations_agent.agent_schemas import Identifier, Status
from ai_operations_agent.recommendations import build_recommendations
from ai_operations_agent.schemas import ActionRecommendation
from ai_operations_agent.tool_schemas import QueueRecommendationsResult
from ai_operations_agent.triage_schemas import (
    InternalValue,
    QueueCount,
    QueueID,
    QueueIDs,
    QueueTriageResult,
    RankedQueue,
    TriageAssessment,
    TriageRiskPolicy,
    TriageScope,
)

MAX_SOURCES = 2
MAX_REFERENCES = 38  # summary + 32 queues + one linked assessment + four recommendations
MAX_SOURCE_BYTES = 131072
MAX_RESPONSE_BYTES = 524288


def _reject_hidden_fields(value):
    # Catalog models predate revalidate_instances="always". model_copy can bypass
    # extra="forbid" and model_dump would silently omit those unknown attributes.
    if isinstance(value, BaseModel):
        if value.__pydantic_extra__ or value.__dict__.keys() - type(value).model_fields.keys():
            raise ValueError("unexpected source fields")
        for child in value.__dict__.values():
            _reject_hidden_fields(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            _reject_hidden_fields(child)


class TriageSummary(InternalValue):
    evaluated_at: AwareDatetime
    ranking_policy_version: Literal["queue-triage-v1"]
    risk_policy: TriageRiskPolicy
    scope: TriageScope
    unavailable_queue_ids: QueueIDs
    available_queue_count: QueueCount
    unavailable_queue_count: QueueCount
    eligible_queue_count: QueueCount
    detail_target_queue_id: QueueID | None
    outcome: Literal["no_available_queues", "no_triggered_rules", "attention_required"]


class TriageSnapshot(InternalValue):
    # A lossless, versioned projection of QueueTriageResult, giving real JSON pointers.
    summary: TriageSummary
    queues: tuple[RankedQueue, ...] = Field(max_length=32)

    def triage_result(self) -> QueueTriageResult:
        value = self.summary.model_dump(mode="json")
        value["rankings"] = [row.model_dump(mode="json") for row in self.queues]
        return QueueTriageResult.model_validate_json(
            json.dumps(value, allow_nan=False), strict=True
        )

    @model_validator(mode="after")
    def canonical_result(self):
        self.triage_result()
        return self


_RECOMMENDATIONS = TypeAdapter(tuple[ActionRecommendation, ...])


class LinkedDetailSnapshot(InternalValue):
    recommendation_policy_version: Literal["queue-recommendations-v1"]
    advisory_only: Literal[True]
    requires_human_review: Literal[True]
    assessment: TriageAssessment
    recommendations: tuple[ActionRecommendation, ...] = Field(max_length=4)

    @field_validator("advisory_only", "requires_human_review", mode="before")
    @classmethod
    def boolean_flags(cls, value):
        if type(value) is not bool:
            raise ValueError("invalid safety flags")
        return value

    @field_validator("recommendations", mode="before")
    @classmethod
    def strict_recommendations(cls, value, info):
        # The existing catalog has immutable children; strict validation is recursive.
        if info.mode == "json":
            return _RECOMMENDATIONS.validate_json(json.dumps(value, allow_nan=False), strict=True)
        return _RECOMMENDATIONS.validate_python(value, strict=True)

    @model_validator(mode="after")
    def canonical_recommendations(self):
        _reject_hidden_fields(self)
        QueueRecommendationsResult.model_validate_json(self.model_dump_json(), strict=True)
        a = self.assessment
        expected = build_recommendations(
            a.metrics.risk_metrics(),
            TriageRiskPolicy(
                sla_at_risk_remaining_percent=a.policy.sla_at_risk_remaining_percent,
                unassigned_backlog_min_tickets=a.policy.unassigned_backlog_min_tickets,
            ).risk_policy(),
            a.policy.sla_at_risk_window_seconds,
            a.risk_evaluation(),
        )
        if tuple(r.model_dump(mode="json") for r in self.recommendations) != tuple(
            r.model_dump(mode="json") for r in expected
        ):
            raise ValueError("noncanonical recommendations")
        return self


class BoundedEvidenceSource(InternalValue):
    @model_validator(mode="after")
    def bounded_snapshot(self):
        _reject_hidden_fields(self)
        if len(self.model_dump_json().encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError("source exceeds size limit")
        return self


class TriageSource(BoundedEvidenceSource):
    schema_version: Literal["triage-evidence-v2"] = "triage-evidence-v2"
    source_id: Identifier
    run_id: Identifier
    tool_name: Literal["get_queue_triage"]
    snapshot: TriageSnapshot


class LinkedDetailSource(BoundedEvidenceSource):
    schema_version: Literal["triage-evidence-v2"] = "triage-evidence-v2"
    source_id: Identifier
    run_id: Identifier
    tool_name: Literal["get_queue_recommendations"]
    parent_triage_source_id: Identifier
    snapshot: LinkedDetailSnapshot


TriageEvidenceSource = Annotated[
    TriageSource | LinkedDetailSource, Field(discriminator="tool_name")
]


class TriageReference(InternalValue):
    source_id: Identifier
    kind: Literal["triage_summary", "triage_queue", "assessment", "recommendation"]
    pointer: str = Field(min_length=1, max_length=128)


class TriageEvidenceItem(InternalValue):
    reference: TriageReference
    # Summary must explicitly carry null; queue evidence must carry a positive ID.
    queue_id: QueueID | None
    value: JsonValue

    @model_validator(mode="after")
    def queue_level(self):
        if (self.reference.kind == "triage_summary") != (self.queue_id is None):
            raise ValueError("incorrect evidence level")
        return self


class TriageSelection(InternalValue):
    """Pure bounded selection contract for offline tests, never a model turn."""

    references: tuple[TriageReference, ...] = Field(max_length=MAX_REFERENCES)


class TriageExplanationBlock(InternalValue):
    kind: Literal[
        "triage_summary",
        "ranked_queue",
        "recommendations",
        "no_available_queues",
        "no_rules",
        "inability",
    ]
    text: str = Field(min_length=1, max_length=4096)
    references: tuple[TriageReference, ...] = Field(max_length=MAX_REFERENCES)


class TriageFailure(InternalValue):
    code: Literal["invalid_evidence", "size_limit", "tool_unavailable", "access_denied"]
    message: str = Field(max_length=80)


class TriageAgentResponse(InternalValue):
    schema_version: Literal["offline-agent-v2"] = "offline-agent-v2"
    run_id: Identifier
    status: Status
    read_only: Literal[True] = True
    advisory_only: Literal[True] = True
    requires_human_review: Literal[True] = True
    sources: tuple[TriageEvidenceSource, ...] = Field(default=(), max_length=MAX_SOURCES)
    evidence: tuple[TriageEvidenceItem, ...] = Field(default=(), max_length=MAX_REFERENCES)
    explanation: tuple[TriageExplanationBlock, ...] = Field(default=(), max_length=39)
    uncertainty: tuple[str, ...] = Field(default=(), max_length=3)
    error: TriageFailure | None = None

    @field_validator("read_only", "advisory_only", "requires_human_review", mode="before")
    @classmethod
    def boolean_flags(cls, value):
        if type(value) is not bool:
            raise ValueError("invalid safety flags")
        return value

    @model_validator(mode="after")
    def grounded_contract(self):
        from ai_operations_agent.triage_evidence import _validate_triage_response_contract

        _validate_triage_response_contract(self)
        return self
