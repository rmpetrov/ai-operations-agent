"""Closed internal rejection labels; never retain candidate or exception contents."""

from enum import StrEnum

from ai_operations_agent.agent_evidence import GroundingError


class RejectionReason(StrEnum):
    INVALID_ANSWER_SELECTION = "invalid_answer_selection"
    RECOMMENDATION_SELECTION_MISMATCH = "recommendation_selection_mismatch"
    ASSESSMENT_REFERENCE_LAYOUT_MISMATCH = "assessment_reference_layout_mismatch"
    EXPLANATION_BLOCKS_MISMATCH = "explanation_blocks_mismatch"
    RESPONSE_CONTRACT_VALIDATION_FAILURE = "response_contract_validation_failure"
    INTERNAL_VALIDATION_RENDERING_FAILURE = "internal_validation_rendering_failure"


class AnswerRejection(GroundingError):
    def __init__(self, reason: RejectionReason):
        self.reason = reason
        super().__init__()  # No candidate, exception text, or operational identifiers.


def sanitized_rejection_reason(value) -> str | None:
    # Require the enum itself: arbitrary strings/exception messages never cross this boundary.
    return value.value if type(value) is RejectionReason else None
