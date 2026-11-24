"""Request-local v2 metadata. All values are fixed labels, never operational data."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class Stage(StrEnum):
    MODEL = "model_attempt"
    PROPOSED = "tool_proposed"
    ACCEPTED = "tool_accepted"
    REJECTED = "tool_rejected"
    SNAPSHOT = "triage_snapshot_dispatch"
    SNAPSHOT_COMPLETE = "triage_snapshot_completed"
    PARENT = "parent_capture"
    DETAIL = "snapshot_detail_dispatch"
    LINK = "linked_capture"
    COMPOSE = "server_owned_composition"
    COMPLETE = "workflow_completeness"
    PUBLIC = "public_validation"
    PRIVATE = "private_validation"
    FINAL = "final_outcome"


class Reason(StrEnum):
    REQUEST = "invalid_request"
    CONTEXT = "invalid_context"
    LIMITS = "invalid_limits"
    GRANT = "missing_grant"
    SCOPE = "invalid_scope"
    SCOPE_LIMIT = "scope_too_large"
    QUEUE = "queue_unavailable"
    TARGET = "wrong_detail_target"
    MODEL = "adapter_unavailable"
    REFUSAL = "model_refusal"
    TURN = "malformed_turn"
    ANSWER = "premature_answer"
    UNKNOWN = "unknown_tool"
    SEQUENCE = "wrong_sequence"
    DUPLICATE = "repeated_call"
    ARGUMENTS = "invalid_arguments"
    MODEL_BUDGET = "model_budget_exceeded"
    TOOL_BUDGET = "tool_budget_exceeded"
    TIME = "deadline_exceeded"
    DB = "snapshot_unavailable"
    SNAPSHOT = "invalid_snapshot"
    PARENT = "invalid_parent_capture"
    CONTINUATION = "invalid_continuation"
    DETAIL = "detail_construction_failed"
    LINK = "invalid_linked_detail"
    COMPOSE = "composition_failed"
    COMPLETE = "incomplete_workflow"
    PUBLIC = "invalid_public_contract"
    PRIVATE = "invalid_private_provenance"
    SIZE = "size_limit"

    @property
    def public_code(self):
        if self is Reason.SIZE:
            return "size_limit"
        if self in {Reason.GRANT, Reason.SCOPE, Reason.SCOPE_LIMIT, Reason.QUEUE, Reason.TARGET}:
            return "access_denied"
        if self in {Reason.MODEL, Reason.REFUSAL, Reason.MODEL_BUDGET, Reason.TOOL_BUDGET,
                    Reason.TIME, Reason.DB, Reason.DETAIL}:
            return "tool_unavailable"
        return "invalid_evidence"


ToolLabel = Literal["get_queue_triage", "get_queue_recommendations", "list_queues", "unknown"]


@dataclass(frozen=True, slots=True)
class TriageEvent:
    stage: Stage
    tool: ToolLabel | None = None


@dataclass(slots=True)
class TriageRuntimeTrace:
    """Replaced at the end of one execution; only the fixed transition sequence can emit events."""

    events: tuple[TriageEvent, ...] = ()
    status: Literal["answered", "error"] | None = None
    reason: Reason | None = None
