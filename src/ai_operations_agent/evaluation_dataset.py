"""Versioned, independently authored expectations; never populated from agent responses."""

from importlib.resources import files
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ai_operations_agent.agent_schemas import AgentRequest, Status
from ai_operations_agent.tool_schemas import StrictValue, ToolName

QueueKey = Literal["empty", "healthy", "overdue", "upcoming", "unassigned", "offline",
                   "all", "ambiguous_a", "ambiguous_b", "hidden"]
RecommendationCode = Literal["REVIEW_OVERDUE_TICKETS", "REVIEW_UPCOMING_DEADLINES",
                             "VERIFY_RECORDED_AGENT_STATUS", "TRIAGE_UNASSIGNED_TICKETS"]
# This is an expectation specification, not imported from the engine under evaluation.
CANONICAL_CODES = ("REVIEW_OVERDUE_TICKETS", "REVIEW_UPCOMING_DEADLINES",
                   "VERIFY_RECORDED_AGENT_STATUS", "TRIAGE_UNASSIGNED_TICKETS")
Behavior = Literal["demo", "unsupported", "invalid_tool", "unauthorized_call", "wrong_queue",
                   "invalid_arguments", "duplicate_request", "fabricated_evidence",
                   "incomplete_recommendations", "reordered_recommendations", "malformed",
                   "timeout", "unavailable", "refusal"]


class ExpectedCall(StrictValue):
    name: ToolName
    queue_key: QueueKey | None = None

    @model_validator(mode="after")
    def correct_shape(self):
        if (self.name == "get_queue_recommendations") != (self.queue_key is not None):
            raise ValueError("assessment requires a queue; discovery takes no arguments")
        return self

    def arguments(self, queue_ids):
        return {} if self.queue_key is None else {"queue_id": queue_ids[self.queue_key]}


class Expectation(StrictValue):
    status: Status
    error_code: Literal["queue_unavailable", "unsupported_tool", "invalid_arguments",
                        "invalid_transition", "invalid_answer", "invalid_model_turn",
                        "deadline_exceeded", "model_unavailable"] | None = None
    allowed_tool_sequences: tuple[tuple[ExpectedCall, ...], ...] = Field(min_length=1)
    recommendation_codes: tuple[RecommendationCode, ...] | None
    security_properties: tuple[Literal["authorized_dispatch_only", "no_operational_writes",
                                       "grounded_response", "no_operational_claims"], ...]

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "error") != (self.error_code is not None):
            raise ValueError("explicit error classification required")
        codes = self.recommendation_codes
        if codes is not None and codes != tuple(code for code in CANONICAL_CODES if code in codes):
            raise ValueError("expected recommendations must be unique and canonical")
        if not {"authorized_dispatch_only", "no_operational_writes", "grounded_response"}.issubset(
            self.security_properties
        ):
            raise ValueError("security invariants are mandatory")
        if self.status != "answered" and "no_operational_claims" not in self.security_properties:
            raise ValueError("nonanswers must prohibit operational claims")
        return self


class Scenario(StrictValue):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    category: Literal["task_success", "security", "failure"]
    request: AgentRequest
    authorized_queue_scope: tuple[QueueKey, ...] = Field(min_length=1)
    selected_queue: QueueKey | None = None
    behavior: Behavior
    expected: Expectation

    @model_validator(mode="after")
    def validate_expectations(self):
        if len(set(self.authorized_queue_scope)) != len(self.authorized_queue_scope):
            raise ValueError("duplicate scope")
        if self.category == "task_success" and self.expected.status != "answered":
            raise ValueError("normal success cases must answer")
        for sequence in self.expected.allowed_tool_sequences:
            names = tuple(call.name for call in sequence)
            if len(names) > 2 or len(set(names)) != len(names):
                raise ValueError("invalid expected sequence")
            for call in sequence:
                if call.queue_key is not None and (
                    call.queue_key not in self.authorized_queue_scope
                    or self.selected_queue is not None and call.queue_key != self.selected_queue
                ):
                    raise ValueError("expectations cannot permit unauthorized dispatch")
        assessment = any(call.queue_key is not None
                         for seq in self.expected.allowed_tool_sequences for call in seq)
        if assessment != (self.expected.recommendation_codes is not None):
            raise ValueError("assessment expectations require explicit recommendation membership")
        return self


class EvaluationDataset(StrictValue):
    version: Literal["synthetic-agent-eval-v1"]
    fixture_version: Literal["synthetic-queues-v1"]
    scenarios: tuple[Scenario, ...] = Field(min_length=15)

    @model_validator(mode="after")
    def unique_scenarios(self):
        if len({s.id for s in self.scenarios}) != len(self.scenarios):
            raise ValueError("duplicate scenario ID")
        if {s.category for s in self.scenarios} != {"task_success", "security", "failure"}:
            raise ValueError("normal and negative categories are required")
        return self


def load_dataset() -> EvaluationDataset:
    return EvaluationDataset.model_validate_json(
        files("ai_operations_agent").joinpath("evaluation_data/v1.json").read_text(), strict=True,
    )
