"""Deterministic offline simulation; question text is never interpreted as intent."""

from time import monotonic

from ai_operations_agent.agent_schemas import (
    AgentRequest,
    EvidenceReference,
    ModelFailure,
    ModelTurn,
    StructuredAnswerDraft,
    ToolRequest,
)


class DeterministicDemoAdapter:
    """Select fixed workflows using only the runtime's detached conversation.

    This ModelAdapter has no database, dispatcher, provider, or permissions capability.
    The runtime independently checks selection, transitions, and every final reference.
    """

    def __init__(self, selected_queue_id: int | None = None):
        self._selected_queue_id = selected_queue_id

    def next_turn(
        self, conversation: tuple[dict, ...], permitted_tools: tuple[str, ...],
        response_schema: dict, deadline: float,
    ) -> ModelTurn:
        if monotonic() >= deadline:
            raise TimeoutError
        request = AgentRequest.model_validate(conversation[0]["request"], strict=True)
        results = [message for message in conversation if message["role"] == "tool"]
        latest = results[-1] if results else None
        if latest is not None:
            source = latest["source"]
            references = tuple(EvidenceReference.model_validate(ref, strict=True)
                               for ref in latest["references"])
            if source["tool_name"] == "get_queue_recommendations":
                codes = tuple(item["code"] for item in source["snapshot"]["recommendations"])
                return StructuredAnswerDraft(
                    kind="queue_focus", recommendation_codes=codes,
                    references=tuple(ref for ref in references
                                     if ref.kind in {"assessment", "recommendation"}),
                    blocks=("queue_summary", "recommendations" if codes else "no_rules"),
                )
            if request.workflow == "discovery" and source["tool_name"] == "list_queues":
                return StructuredAnswerDraft(kind="discovery", references=references,
                                             blocks=("discovery",))
        if permitted_tools == ("list_queues",):
            return ToolRequest(call_id="demo_list", name="list_queues", arguments={})
        if permitted_tools == ("get_queue_recommendations",):
            target = self._selected_queue_id
            if target is None and latest is not None and request.queue_name is not None:
                matches = [queue["id"] for queue in latest["source"]["snapshot"]
                           if queue["name"].strip().casefold() == request.queue_name.strip().casefold()]
                if len(matches) == 1:
                    target = matches[0]
            if target is not None:
                return ToolRequest(call_id="demo_assess", name="get_queue_recommendations",
                                   arguments={"queue_id": target})
        return ModelFailure(reason="unavailable")
