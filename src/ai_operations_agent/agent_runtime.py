"""Synchronous offline state machine: at most two serial reads and three model turns."""

import json
import math
from collections.abc import Callable
from contextlib import ExitStack
from copy import deepcopy
from time import monotonic
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.engine import Engine

from ai_operations_agent import tools
from ai_operations_agent.agent_evidence import EvidenceLedger, GroundingError
from ai_operations_agent.agent_schemas import (
    AgentRequest,
    AgentResponse,
    ExplanationBlock,
    ModelFailure,
    ModelTurn,
    Refusal,
    RuntimeLimits,
    SanitizedError,
    StructuredAnswerDraft,
    ToolRequest,
)
from ai_operations_agent.agent_telemetry import RuntimeTrace
from ai_operations_agent.answer_diagnostics import AnswerRejection, RejectionReason
from ai_operations_agent.fake_model import ModelAdapter, RunScopedModelAdapter
from ai_operations_agent.tool_schemas import (
    AccessContext,
    ExecutionLimits,
    ListQueuesArguments,
    QueueRecommendationsArguments,
)

_TURN = TypeAdapter(ModelTurn)
_MESSAGES = {
    "invalid_request": "invalid agent request",
    "request_too_large": "agent request exceeds size limit",
    "access_denied": "tool access denied",
    "invalid_model_turn": "invalid model outcome",
    "invalid_transition": "invalid agent transition",
    "unsupported_tool": "unsupported tool",
    "invalid_arguments": "invalid tool arguments",
    "queue_unavailable": "queue unavailable",
    "database_unavailable": "database unavailable",
    "tool_failure": "tool unavailable",
    "model_unavailable": "model unavailable",
    "deadline_exceeded": "execution deadline exhausted",
    "tool_budget_exceeded": "tool request budget exhausted",
    "model_budget_exceeded": "model turn budget exhausted",
    "invalid_answer": "answer failed evidence validation",
    "size_limit": "agent output exceeds size limit",
}
_TOOL_ERRORS = {
    "unsupported tool": "unsupported_tool", "invalid tool arguments": "invalid_arguments",
    "tool arguments exceed size limit": "size_limit", "tool access denied": "access_denied",
    "queue unavailable": "queue_unavailable", "database unavailable": "database_unavailable",
    "tool result exceeds size limit": "size_limit",
}
_INABILITY = {
    "unsupported_capability": ("unsupported", "This request requires an unsupported capability."),
    "missing_queue": ("insufficient_information", "No matching authorized queue is available."),
    "ambiguous_queue": ("needs_clarification", "Select one queue explicitly before assessment."),
    "model_refusal": ("unsupported", "The model declined this request."),
}
_LIMITATIONS = (
    "Aggregate evidence cannot identify tickets or establish intersections; do not sum overlapping counts.",
    "Recorded agent presence does not establish availability or capacity.",
    "This snapshot is not a forecast or a guarantee of operational health.",
)


class RuntimeFailure(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _inability(run_id, ledger, category):
    status, message = _INABILITY[category]
    return AgentResponse(
        run_id=run_id, status=status, sources=ledger.sources,
        explanation=(ExplanationBlock(kind="inability", text=message),), uncertainty=(category,),
    )


def _render(draft, request, ledger, discovered, assessed, target):
    if draft.kind == "inability":
        # Queue matching failures are decided from discovery by the server, never guessed.
        if (request.workflow != "unsupported" or draft.inability != "unsupported_capability"
                or draft.references or draft.recommendation_codes or draft.blocks):
            raise AnswerRejection(RejectionReason.INVALID_ANSWER_SELECTION)
        return _inability(ledger.run_id, ledger, draft.inability)
    if draft.inability is not None or len(set(draft.references)) != len(draft.references):
        raise AnswerRejection(RejectionReason.INVALID_ANSWER_SELECTION)
    if draft.kind == "discovery":
        if request.workflow != "discovery" or discovered is None or assessed is not None:
            raise AnswerRejection(RejectionReason.INVALID_ANSWER_SELECTION)
        expected = ledger.references(discovered.source_id)
        if draft.references != expected or draft.recommendation_codes:
            raise AnswerRejection(RejectionReason.INVALID_ANSWER_SELECTION)
        if draft.blocks != ("discovery",):
            raise AnswerRejection(RejectionReason.EXPLANATION_BLOCKS_MISMATCH)
        # Names and IDs are data interpolated by the server, never instructions.
        names = "; ".join(f"{q['name']} (queue {q['id']})" for q in discovered.snapshot)
        blocks = (ExplanationBlock(kind="discovery", text=(
            f"Authorized queues: {names}." if names else "No authorized queues were discovered."
        ), references=expected),)
        uncertainty = ("Only queues in the server-authorized scope are shown.",)
    else:
        if request.workflow != "queue_assessment" or assessed is None or target is None:
            raise AnswerRejection(RejectionReason.INVALID_ANSWER_SELECTION)
        snapshot = assessed.snapshot
        canonical = snapshot["recommendations"]
        if draft.recommendation_codes != tuple(item["code"] for item in canonical):
            raise AnswerRejection(RejectionReason.RECOMMENDATION_SELECTION_MISMATCH)
        refs = ledger.references(assessed.source_id)
        required = tuple(ref for ref in refs if ref.kind in {"assessment", "recommendation"})
        # A compact answer always cites its complete assessment and every recommendation.
        # Additional metric references may follow, from this assessment and this queue only.
        if draft.references[:len(required)] != required or any(
            ref not in refs or ref.kind != "metric" for ref in draft.references[len(required):]
        ):
            raise AnswerRejection(RejectionReason.ASSESSMENT_REFERENCE_LAYOUT_MISMATCH)
        expected_blocks = ("queue_summary", "recommendations" if canonical else "no_rules")
        if draft.blocks != expected_blocks:
            raise AnswerRejection(RejectionReason.EXPLANATION_BLOCKS_MISMATCH)
        assessment = snapshot["assessment"]
        metrics = assessment["metrics"]
        blocks = [ExplanationBlock(
            kind="queue_summary", references=required[:1], text=(
                f"{assessment['queue_name']} (queue {target}), evaluated at {assessment['evaluated_at']}: "
                f"{metrics['active_tickets']} active tickets; recorded severity {assessment['overall_severity']}."
            ),
        )]
        if canonical:
            for item, ref in zip(canonical, required[1:], strict=True):
                blocks.append(ExplanationBlock(kind="recommendations", references=(ref,), text=(
                    f"{item['code']} [{item['priority']}]: {item['suggested_action']} {item['explanation']}"
                )))
        else:
            blocks.append(ExplanationBlock(kind="no_rules", references=required[:1], text=(
                "No configured recommendation rule triggered. This does not guarantee operational health."
            )))
        blocks = tuple(blocks)
        uncertainty = _LIMITATIONS
    evidence = tuple(ledger.resolve(ref, target if assessed is not None else None)
                     for ref in draft.references)
    try:
        return AgentResponse(run_id=ledger.run_id, status="answered", sources=ledger.sources,
                             evidence=evidence, explanation=blocks, uncertainty=uncertainty)
    except (GroundingError, ValidationError):
        raise AnswerRejection(RejectionReason.RESPONSE_CONTRACT_VALIDATION_FAILURE) from None


def run_agent(
    request: AgentRequest, access: AccessContext, adapter: ModelAdapter, *,
    engine: Engine | None = None, limits: RuntimeLimits | None = None,
    clock: Callable[[], float] = monotonic, new_id: Callable[[], str] = lambda: uuid4().hex,
    trace: RuntimeTrace | None = None,
    evaluation_context: tools.EvaluationContext | None = None,
) -> AgentResponse:
    """Trusted Python entry point; no HTTP endpoint, CLI, script parser, or persistence.

    Workflow is selected by application code. Queue names use exact Unicode casefolded,
    stripped matching; the fake model's routing choice cannot override that resolution.
    Deadlines are cooperative at the adapter boundary and checked around every operation.
    """
    limits = limits or RuntimeLimits()
    run_id = new_id()
    ledger = EvidenceLedger(run_id, access, new_id)
    discovered = assessed = None
    target = access.selected_queue_id if isinstance(access, AccessContext) else None
    calls: set[str] = set()
    used: set[str] = set()
    model_scope = ExitStack()
    pending_tool = None

    def reject_pending():
        nonlocal pending_tool
        if trace is not None and pending_tool is not None:
            trace.event("tool_rejected", name=pending_tool)
        pending_tool = None

    def failure(code):
        return AgentResponse(run_id=run_id, status="error", sources=ledger.sources,
                             error=SanitizedError(code=code, message=_MESSAGES[code]))

    def finish(response):
        # Compare resolved evidence against our private ledger, not model-authored payloads.
        if response.sources != ledger.sources or any(
            item != ledger.resolve(item.reference) for item in response.evidence
        ):
            raise AnswerRejection(RejectionReason.RESPONSE_CONTRACT_VALIDATION_FAILURE)
        if len(response.model_dump_json().encode("utf-8")) > limits.max_response_bytes:
            # Oversize failure is atomic; no partial facts or truncated source snapshots.
            response = AgentResponse(run_id=run_id, status="error", error=SanitizedError(
                code="size_limit", message=_MESSAGES["size_limit"],
            ))
        if trace is not None:
            trace.final_status = response.status
        return response

    try:
        if not isinstance(request, AgentRequest):
            raise RuntimeFailure("invalid_request")
        if len(_json_bytes(request.model_dump())) > limits.max_question_bytes:
            raise RuntimeFailure("request_too_large")
        if not isinstance(access, AccessContext) or not access.allowed_queue_ids:
            raise RuntimeFailure("access_denied")
        if target is not None and target not in access.allowed_queue_ids:
            raise RuntimeFailure("queue_unavailable")
        if request.workflow == "queue_assessment" and target is None and not (
            request.queue_name and request.queue_name.strip()
        ):
            return finish(_inability(run_id, ledger, "ambiguous_queue"))
        start = clock()
        if not math.isfinite(start):
            raise RuntimeFailure("deadline_exceeded")
        deadline = start + limits.timeout_seconds

        def remaining():
            now = clock()
            if not math.isfinite(now) or now < start or now >= deadline:
                raise RuntimeFailure("deadline_exceeded")
            return deadline - now

        conversation = [{"role": "user", "request": request.model_dump()}]
        try:
            if isinstance(adapter, RunScopedModelAdapter):
                adapter = model_scope.enter_context(adapter.open_run())
        except Exception:
            raise RuntimeFailure("model_unavailable") from None
        for _ in range(limits.max_model_turns):
            remaining()
            if request.workflow == "unsupported" or assessed is not None:
                permitted = ()
            elif discovered is None and (target is None or request.workflow == "discovery"):
                permitted = ("list_queues",)
            elif request.workflow == "queue_assessment":
                permitted = ("get_queue_recommendations",)
            else:
                permitted = ()
            permitted = tuple(name for name in permitted if name in access.allowed_tool_names)
            if not access.allowed_tool_names and request.workflow != "unsupported":
                raise RuntimeFailure("access_denied")
            if len(_json_bytes(conversation)) > limits.max_conversation_bytes:
                raise RuntimeFailure("size_limit")
            try:
                turn = adapter.next_turn(
                    deepcopy(tuple(conversation)), permitted,
                    StructuredAnswerDraft.model_json_schema(), deadline,
                )
            except TimeoutError:
                raise RuntimeFailure("deadline_exceeded") from None
            except Exception:
                raise RuntimeFailure("model_unavailable") from None
            if trace is not None:
                trace.model_turn(turn)
                raw_turn = turn.model_dump() if isinstance(turn, ToolRequest) else turn
                if type(raw_turn) is dict and raw_turn.get("type") == "tool_request":
                    pending_tool = raw_turn.get("name", "unsupported")
                    if type(pending_tool) is not str:
                        pending_tool = "unsupported"
            remaining()
            try:
                raw = turn.model_dump() if isinstance(turn, (ToolRequest, StructuredAnswerDraft,
                                                             Refusal, ModelFailure)) else turn
                if len(_json_bytes(raw)) > limits.max_turn_bytes:
                    raise RuntimeFailure("size_limit")
                turn = _TURN.validate_python(raw, strict=True)
            except (ValidationError, TypeError, ValueError, RecursionError, UnicodeError) as exc:
                if isinstance(exc, RuntimeFailure):
                    raise
                raise RuntimeFailure("invalid_model_turn") from None
            if isinstance(turn, Refusal):
                return finish(_inability(run_id, ledger, "model_refusal"))
            if isinstance(turn, ModelFailure):
                raise RuntimeFailure("deadline_exceeded" if turn.reason == "timeout"
                                     else "model_unavailable")
            if isinstance(turn, StructuredAnswerDraft):
                try:
                    response = _render(turn, request, ledger, discovered, assessed, target)
                    remaining()
                    return finish(response)
                except (AnswerRejection, RuntimeFailure):
                    raise
                except Exception:
                    raise AnswerRejection(
                        RejectionReason.INTERNAL_VALIDATION_RENDERING_FAILURE,
                    ) from None
            # Validate the complete single call before dispatch; no batches, retries or repairs.
            if turn.name not in tools.SUPPORTED_TOOLS:
                raise RuntimeFailure("unsupported_tool")
            if turn.name not in access.allowed_tool_names:
                raise RuntimeFailure("access_denied")
            if turn.call_id in calls or turn.name in used:
                raise RuntimeFailure("invalid_transition")
            if len(calls) >= limits.max_tool_requests:
                raise RuntimeFailure("tool_budget_exceeded")
            if turn.name not in permitted:
                raise RuntimeFailure("invalid_transition")
            try:
                payload = tools._arguments(turn.arguments, access.limits.max_argument_bytes)
                if turn.name == "list_queues":
                    ListQueuesArguments.model_validate(payload, strict=True)
                else:
                    args = QueueRecommendationsArguments.model_validate(payload, strict=True)
                    if args.queue_id not in access.allowed_queue_ids or args.queue_id != target:
                        raise RuntimeFailure("queue_unavailable")
            except (ValidationError, tools.ToolError):
                raise RuntimeFailure("invalid_arguments") from None
            calls.add(turn.call_id)
            used.add(turn.name)
            if trace is not None:
                trace.event("tool_accepted", name=turn.name, arguments=payload)
            pending_tool = None
            # A PostgreSQL statement may consume only the remaining run budget.
            tool_limits = ExecutionLimits(
                max_argument_bytes=access.limits.max_argument_bytes,
                max_result_bytes=access.limits.max_result_bytes,
                statement_timeout_ms=min(access.limits.statement_timeout_ms,
                                         max(1, int(remaining() * 1000))),
            )
            scoped = access.model_copy(update={"limits": tool_limits})
            try:
                if trace is not None:
                    trace.event("tool_dispatched", name=turn.name, arguments=payload)
                extra = {"evaluation_context": evaluation_context} if evaluation_context else {}
                snapshot = tools.execute_tool(turn.name, payload, scoped, engine=engine, **extra)
            except tools.ToolError as exc:
                raise RuntimeFailure(_TOOL_ERRORS.get(str(exc), "tool_failure")) from None
            except Exception:
                raise RuntimeFailure("tool_failure") from None
            remaining()  # Expired tool results cannot become evidence.
            source = ledger.capture(turn.name, snapshot)
            if trace is not None:
                trace.event("tool_completed", name=turn.name, snapshot=snapshot)
            if turn.name == "list_queues":
                discovered = source
                if request.workflow == "queue_assessment":
                    matches = [q["id"] for q in source.snapshot
                               if q["name"].strip().casefold() == request.queue_name.strip().casefold()]
                    if len(matches) != 1:
                        return finish(_inability(run_id, ledger,
                                                 "ambiguous_queue" if matches else "missing_queue"))
                    target = matches[0]
            else:
                if source.snapshot["assessment"]["queue_id"] != target:
                    raise GroundingError("wrong assessment queue")
                assessed = source
            conversation += [{"role": "assistant", "outcome": turn.model_dump()}, {
                "role": "tool", "call_id": turn.call_id, "source": source.model_dump(),
                "references": [ref.model_dump() for ref in ledger.references(source.source_id)],
            }]
        raise RuntimeFailure("model_budget_exceeded")
    except RuntimeFailure as exc:
        reject_pending()
        return finish(failure(exc.code))
    except AnswerRejection as exc:
        reject_pending()
        if trace is not None:
            trace.rejection_reason = exc.reason
        return finish(failure("invalid_answer"))
    except (GroundingError, ValidationError, TypeError, ValueError, RecursionError, UnicodeError):
        reject_pending()
        if trace is not None:
            trace.rejection_reason = RejectionReason.INTERNAL_VALIDATION_RENDERING_FAILURE
        return finish(failure("invalid_answer"))
    finally:
        model_scope.close()
