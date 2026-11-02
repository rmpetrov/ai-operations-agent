"""Offline runtime contracts. Requests/permissions come from trusted application code."""

from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from ai_operations_agent.tool_schemas import PositiveInt, StrictValue, ToolName

Status = Literal["answered", "needs_clarification", "insufficient_information", "unsupported", "error"]
BlockKind = Literal["discovery", "queue_summary", "recommendations", "no_rules", "inability"]
Inability = Literal["unsupported_capability", "missing_queue", "ambiguous_queue", "model_refusal"]
ReferenceKind = Literal["queue", "assessment", "metric", "recommendation"]
Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class AgentRequest(StrictValue):
    question: str = Field(min_length=1)
    # This explicit application routing is not a natural-language intent classifier.
    workflow: Literal["discovery", "queue_assessment", "unsupported"]
    queue_name: str | None = Field(default=None, max_length=160)


class RuntimeLimits(StrictValue):
    max_tool_requests: Annotated[int, Field(strict=True, ge=1, le=2)] = 2
    max_model_turns: Annotated[int, Field(strict=True, ge=1, le=3)] = 3
    max_question_bytes: PositiveInt = 4096
    max_turn_bytes: PositiveInt = 8192
    max_conversation_bytes: PositiveInt = 196608
    max_response_bytes: Annotated[int, Field(strict=True, ge=1024)] = 196608
    timeout_seconds: float = Field(default=10.0, gt=0, le=30, allow_inf_nan=False)


class ToolRequest(StrictValue):
    type: Literal["tool_request"] = "tool_request"
    call_id: Identifier
    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, JsonValue]


class EvidenceReference(StrictValue):
    source_id: Identifier
    kind: ReferenceKind
    pointer: str = Field(min_length=1, max_length=128)


class StructuredAnswerDraft(StrictValue):
    type: Literal["answer"] = "answer"
    kind: Literal["discovery", "queue_focus", "inability"]
    references: tuple[EvidenceReference, ...] = Field(default=(), max_length=32)
    recommendation_codes: tuple[str, ...] = Field(default=(), max_length=4)
    blocks: tuple[BlockKind, ...] = Field(default=(), max_length=3)
    inability: Inability | None = None


class Refusal(StrictValue):
    type: Literal["refusal"] = "refusal"


class ModelFailure(StrictValue):
    type: Literal["model_failure"] = "model_failure"
    reason: Literal["timeout", "unavailable"]


ModelTurn = Annotated[
    ToolRequest | StructuredAnswerDraft | Refusal | ModelFailure, Field(discriminator="type"),
]


class EvidenceSource(StrictValue):
    source_id: Identifier
    run_id: Identifier
    tool_name: ToolName
    # Complete detached canonical snapshot, never supplied by the model.
    snapshot: JsonValue


class EvidenceItem(StrictValue):
    reference: EvidenceReference
    queue_id: PositiveInt
    value: JsonValue


class ExplanationBlock(StrictValue):
    kind: BlockKind
    text: str
    references: tuple[EvidenceReference, ...] = ()


class SanitizedError(StrictValue):
    code: str
    message: str


class AgentResponse(StrictValue):
    schema_version: Literal["offline-agent-v1"] = "offline-agent-v1"
    run_id: Identifier
    status: Status
    read_only: Literal[True] = True
    advisory_only: Literal[True] = True
    requires_human_review: Literal[True] = True
    sources: tuple[EvidenceSource, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    explanation: tuple[ExplanationBlock, ...] = ()
    uncertainty: tuple[str, ...] = ()
    error: SanitizedError | None = None

    @field_validator("read_only", "advisory_only", "requires_human_review", mode="before")
    @classmethod
    def boolean_flags(cls, value):
        if type(value) is not bool:
            raise ValueError("flags must be booleans")
        return value

    @model_validator(mode="after")
    def source_provenance(self):
        from ai_operations_agent.agent_evidence import validate_response_evidence

        sources = {source.source_id: source for source in self.sources}
        if len(sources) != len(self.sources) or any(
            source.run_id != self.run_id for source in self.sources
        ):
            raise ValueError("invalid source provenance")
        refs = [item.reference for item in self.evidence]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate evidence")
        for item in self.evidence:
            if item.reference.source_id not in sources:
                raise ValueError("unknown source")
        if any(ref not in refs for block in self.explanation for ref in block.references):
            raise ValueError("unsupported explanation reference")
        if (self.status == "error") != (self.error is not None):
            raise ValueError("inconsistent failure status")
        validate_response_evidence(self)
        return self
