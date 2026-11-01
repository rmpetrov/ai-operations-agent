"""Strict internal contracts; these are not HTTP endpoints or user-supplied permissions."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_operations_agent.schemas import (
    QueueRecommendations,
    QueueRiskAssessment,
    RiskAssessmentMetrics,
    RiskAssessmentPolicy,
    RiskAssessmentSignal,
)

ToolName = Literal["list_queues", "get_queue_recommendations"]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]


class StrictValue(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ExecutionLimits(StrictValue):
    max_argument_bytes: PositiveInt = 4096
    max_result_bytes: PositiveInt = 65536
    statement_timeout_ms: PositiveInt = 5000


class AccessContext(StrictValue):
    """Construct only in trusted server code, never from a question or tool arguments."""

    allowed_queue_ids: frozenset[PositiveInt] = frozenset()
    allowed_tool_names: frozenset[ToolName] = frozenset()
    selected_queue_id: PositiveInt | None = None
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits)


class ListQueuesArguments(StrictValue):
    pass


class QueueRecommendationsArguments(StrictValue):
    queue_id: PositiveInt


class QueueIdentity(StrictValue):
    id: PositiveInt
    name: str


# Reuse canonical fields/validators while forbidding extras throughout the assessment.
# Validation at the tool boundary uses strict=True recursively, including recommendations.
class ToolAssessmentMetrics(RiskAssessmentMetrics, StrictValue):
    pass


class ToolAssessmentPolicy(RiskAssessmentPolicy, StrictValue):
    pass


class ToolAssessmentSignal(RiskAssessmentSignal, StrictValue):
    pass


class ToolQueueAssessment(QueueRiskAssessment, StrictValue):
    queue_id: PositiveInt
    policy: ToolAssessmentPolicy
    metrics: ToolAssessmentMetrics
    signals: list[ToolAssessmentSignal]


class QueueRecommendationsResult(QueueRecommendations, StrictValue):
    assessment: ToolQueueAssessment

    @field_validator("advisory_only", "requires_human_review", mode="before")
    @classmethod
    def require_boolean_flags(cls, value):
        # Literal[True] alone also accepts numeric 1, even in Pydantic strict mode.
        if type(value) is not bool:
            raise ValueError("advisory flags must be booleans")
        return value
