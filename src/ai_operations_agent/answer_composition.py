"""Trusted final selection from captured evidence; no model data or additional I/O."""

from enum import StrEnum

from ai_operations_agent.agent_evidence import EvidenceLedger
from ai_operations_agent.agent_schemas import AgentRequest, StructuredAnswerDraft
from ai_operations_agent.answer_diagnostics import AnswerRejection, RejectionReason


class CompositionStrategy(StrEnum):
    MODEL_SELECTED = "model_selected"
    SERVER_OWNED = "server_owned"


def compose_answer(
    request: AgentRequest, ledger: EvidenceLedger, *, source_id: str, target: int | None,
) -> StructuredAnswerDraft:
    """Select only from the private ledger, then leave rendering/validation to the runtime.

    The caller must have completed the authorized tool transition. This helper cannot
    dispatch tools, resolve names, repair a model draft, or manufacture an inability.
    """
    source = next((source for source in ledger.sources if source.source_id == source_id), None)
    if source is not None and request.workflow == "discovery" and source.tool_name == "list_queues":
        return StructuredAnswerDraft(
            kind="discovery", references=ledger.references(source_id), blocks=("discovery",),
        )
    if (source is not None and request.workflow == "queue_assessment"
            and source.tool_name == "get_queue_recommendations" and target is not None
            and source.snapshot["assessment"]["queue_id"] == target):
        codes = tuple(item["code"] for item in source.snapshot["recommendations"])
        return StructuredAnswerDraft(
            kind="queue_focus", recommendation_codes=codes,
            references=tuple(ref for ref in ledger.references(source_id)
                             if ref.kind in {"assessment", "recommendation"}),
            blocks=("queue_summary", "recommendations" if codes else "no_rules"),
        )
    raise AnswerRejection(RejectionReason.INVALID_ANSWER_SELECTION)
