"""Trusted, offline cross-queue triage. Only the first dispatch can acquire a database session."""

import json
import math
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from uuid import uuid4

from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy.engine import Engine

from ai_operations_agent import tools
from ai_operations_agent.agent_evidence import GroundingError
from ai_operations_agent.agent_schemas import (
    ModelFailure,
    ModelTurn,
    Refusal,
    RuntimeLimits,
    StructuredAnswerDraft,
    ToolRequest,
)
from ai_operations_agent.queue_triage import TriageError, effective_triage_scope
from ai_operations_agent.tool_schemas import (
    AccessContext,
    ExecutionLimits,
    ListQueuesArguments,
    QueueRecommendationsArguments,
)
from ai_operations_agent.triage_agent_schemas import (
    MAX_RESPONSE_BYTES,
    LinkedDetailSnapshot,
    LinkedDetailSource,
    TriageAgentRequest,
    TriageAgentResponse,
    TriageSource,
    _reject_hidden_fields,
    recommendations_for_assessment,
)
from ai_operations_agent.triage_agent_telemetry import (
    Reason,
    Stage,
    TriageEvent,
    TriageRuntimeTrace,
)
from ai_operations_agent.triage_evidence import (
    TriageEvidenceLedger,
    TriageSizeError,
    build_triage_answer,
    build_triage_inability,
    validate_triage_response,
)
from ai_operations_agent.triage_schemas import TriageScope

_TRIAGE = "get_queue_triage"
_DETAIL = "get_queue_recommendations"
_TURN = TypeAdapter(ModelTurn)


class _Failure(Exception):
    def __init__(self, reason: Reason):
        self.reason = reason
        super().__init__(reason.value)


def _size(value, maximum):
    encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > maximum:
        raise TriageSizeError("payload exceeds size limit")


def _validated(value, cls):
    if type(value) is not cls:
        raise ValueError("wrong trusted input type")
    _reject_hidden_fields(value)
    # A schema-directed dump can silently discard fields from an incorrect nested model.
    fields = vars(value).copy()
    if cls is AccessContext and isinstance(fields.get("limits"), BaseModel):
        fields["limits"] = _validated(fields["limits"], ExecutionLimits)
    return cls.model_validate(fields, strict=True)


@dataclass(frozen=True)
class _Deadline:
    start: float
    end: float

    def remaining(self):
        now = monotonic()
        if not math.isfinite(now) or not self.start <= now < self.end:
            raise _Failure(Reason.TIME)
        return self.end - now


class _ScriptedAdapter:
    """Private per-run sequencer. The control record contains only runtime-authored data."""

    def __init__(self):
        self.stages = iter(("triage", "detail"))

    def next_turn(self, control):
        stage = next(self.stages)
        if control["role"] != "runtime" or control["stage"] != stage:
            raise ValueError("invalid control record")
        if stage == "triage":
            return ToolRequest(name=_TRIAGE, call_id="triage", arguments={})
        return ToolRequest(name=_DETAIL, call_id="detail",
                           arguments={"queue_id": control["target_queue_id"]})

    def close(self):
        self.stages = iter(())


@contextmanager
def _offline_adapter(deadline):
    try:
        adapter = _ScriptedAdapter()
    except TimeoutError:
        raise
    except Exception:
        raise _Failure(Reason.MODEL) from None
    try:
        yield adapter
    finally:
        try:
            adapter.close()
        except TimeoutError:
            raise
        except Exception:
            raise _Failure(Reason.MODEL) from None
        finally:
            deadline.remaining()


@dataclass(frozen=True)
class _Continuation:
    """A single-use binding to one private capture. No business clock or I/O capability."""

    run_id: str
    parent_id: str
    parent_json: str
    target: int
    scope: TriageScope
    deadline: _Deadline

    def consume(self, ledger, target, deadline, max_result_bytes):
        deadline.remaining()
        if self.deadline is not deadline or self.run_id != ledger.run_id:
            raise _Failure(Reason.CONTINUATION)
        try:
            parent = ledger.claim_detail_parent(self.parent_id, self.parent_json)
        except TriageSizeError:
            raise
        except (GroundingError, ValidationError):
            raise _Failure(Reason.CONTINUATION) from None
        summary = parent.snapshot.summary
        if (summary.scope != self.scope or type(target) is not int
                or target != self.target or target != summary.detail_target_queue_id
                or target not in self.scope.effective_queue_ids):
            raise _Failure(Reason.CONTINUATION)
        row = next((r for r in parent.snapshot.queues if r.detail_eligible), None)
        if row is None or row.assessment.queue_id != target:
            raise _Failure(Reason.CONTINUATION)
        detail = LinkedDetailSnapshot(
            recommendation_policy_version="queue-recommendations-v1", advisory_only=True,
            requires_human_review=True, assessment=row.assessment,
            recommendations=recommendations_for_assessment(row.assessment),
        )
        payload = detail.model_dump(mode="json")
        _size(payload, max_result_bytes)
        deadline.remaining()
        return payload


_TOOL_ERRORS = {
    "tool access denied": Reason.GRANT, "queue unavailable": Reason.QUEUE,
    "triage scope exceeds limit": Reason.SCOPE_LIMIT, "database unavailable": Reason.DB,
    "invalid triage context": Reason.CONTEXT, "invalid tool result": Reason.SNAPSHOT,
    "tool result exceeds size limit": Reason.SIZE, "tool arguments exceed size limit": Reason.SIZE,
    "invalid tool arguments": Reason.ARGUMENTS,
}


class _Run:
    def __init__(self):
        self.id = uuid4().hex
        self.events = []
        self.calls = set()
        self.tools = set()
        self.turns = 0

    def operation(self, stage, reason, function, /, *args, **kwargs):
        """Each event counts entry; acceptance of its result requires time remaining."""
        self.deadline.remaining()
        self.events.append(TriageEvent(stage))
        try:
            return function(*args, **kwargs)
        except (_Failure, TriageSizeError, TimeoutError):
            raise
        except tools.ToolError as exc:
            raise _Failure(_TOOL_ERRORS.get(str(exc), reason)) from None
        except (GroundingError, ValidationError):
            raise _Failure(Reason.LINK if stage == Stage.DETAIL else reason) from None
        except Exception:
            raise _Failure(reason) from None
        finally:
            self.deadline.remaining()

    def proposal(self, adapter, name, target=None):
        if name not in self.access.allowed_tool_names:
            raise _Failure(Reason.GRANT)
        if self.turns >= self.limits.max_model_turns:
            raise _Failure(Reason.MODEL_BUDGET)
        if len(self.calls) >= self.limits.max_tool_requests:
            raise _Failure(Reason.TOOL_BUDGET)
        control = {"role": "runtime", "stage": "triage" if name == _TRIAGE else "detail"}
        if target is not None:
            control["target_queue_id"] = target
        _size([control], self.limits.max_conversation_bytes)
        self.turns += 1
        raw = self.operation(Stage.MODEL, Reason.MODEL, adapter.next_turn, control)
        label = None
        if isinstance(raw, ToolRequest) or (type(raw) is dict and raw.get("type") == "tool_request"):
            proposed = vars(raw).get("name") if isinstance(raw, ToolRequest) else raw.get("name")
            label = proposed if type(proposed) is str and proposed in tools.INTERNAL_TOOLS else "unknown"
            self.events.append(TriageEvent(Stage.PROPOSED, label))
        try:
            if isinstance(raw, BaseModel):
                if type(raw) not in {ToolRequest, Refusal, ModelFailure, StructuredAnswerDraft}:
                    raise _Failure(Reason.TURN)
                _reject_hidden_fields(raw)
                raw = raw.model_dump(warnings=False)
            _size(raw, self.limits.max_turn_bytes)
            turn = _TURN.validate_python(raw, strict=True)
            if isinstance(turn, ModelFailure):
                raise _Failure(Reason.TIME if turn.reason == "timeout" else Reason.MODEL)
            if isinstance(turn, Refusal):
                raise _Failure(Reason.REFUSAL)
            if not isinstance(turn, ToolRequest):
                raise _Failure(Reason.ANSWER)
            if turn.name not in tools.INTERNAL_TOOLS:
                raise _Failure(Reason.UNKNOWN)
            if turn.name not in self.access.allowed_tool_names:
                raise _Failure(Reason.GRANT)
            if turn.name in self.tools or turn.call_id in self.calls:
                raise _Failure(Reason.DUPLICATE)
            if turn.name != name:
                raise _Failure(Reason.SEQUENCE)
            payload = tools._arguments(turn.arguments, self.access.limits.max_argument_bytes)
            try:
                if name == _TRIAGE:
                    ListQueuesArguments.model_validate(payload, strict=True)
                else:
                    args = QueueRecommendationsArguments.model_validate(payload, strict=True)
                    if args.queue_id != target or args.queue_id not in self.scope.effective_queue_ids:
                        raise _Failure(Reason.TARGET)
            except ValidationError:
                raise _Failure(Reason.ARGUMENTS) from None
            self.deadline.remaining()
        except Exception as exc:
            if label is not None:
                self.events.append(TriageEvent(Stage.REJECTED, label))
            if isinstance(exc, (_Failure, TriageSizeError)):
                raise
            if isinstance(exc, tools.ToolError):
                raise _Failure(_TOOL_ERRORS.get(str(exc), Reason.ARGUMENTS)) from None
            raise _Failure(Reason.TURN) from None
        self.calls.add(turn.call_id)
        self.tools.add(name)
        self.events.append(TriageEvent(Stage.ACCEPTED, name))
        return payload

    def execute(self, request, access, engine, context, limits):
        start = monotonic()
        try:
            self.limits = RuntimeLimits() if limits is None else _validated(limits, RuntimeLimits)
        except (TypeError, ValueError):
            raise _Failure(Reason.LIMITS) from None
        self.deadline = _Deadline(start, start + self.limits.timeout_seconds)
        self.deadline.remaining()
        try:
            request = _validated(request, TriageAgentRequest)
        except (ValueError, TypeError):
            raise _Failure(Reason.REQUEST) from None
        _size(request.model_dump(), self.limits.max_question_bytes)
        try:
            self.access = _validated(access, AccessContext)
            self.scope = effective_triage_scope(self.access)
        except TriageError as exc:
            raise _Failure(_TOOL_ERRORS.get(str(exc), Reason.SCOPE)) from None
        except (ValueError, TypeError):
            raise _Failure(Reason.SCOPE) from None
        if _TRIAGE not in self.access.allowed_tool_names:
            raise _Failure(Reason.GRANT)
        if not isinstance(engine, Engine) or engine.dialect.name not in {"sqlite", "postgresql"}:
            raise _Failure(Reason.CONTEXT)
        try:
            ledger = TriageEvidenceLedger(self.id, self.access, context, lambda: uuid4().hex)
        except GroundingError:
            raise _Failure(Reason.CONTEXT) from None
        self.deadline.remaining()
        with _offline_adapter(self.deadline) as adapter:
            payload = self.proposal(adapter, _TRIAGE)
            tool_limits = self.access.limits
            timeout_ms = min(tool_limits.statement_timeout_ms, max(1, int(self.deadline.remaining() * 1000)))
            scoped = self.access.model_copy(update={
                "limits": tool_limits.model_copy(update={"statement_timeout_ms": timeout_ms})
            })
            snapshot = self.operation(Stage.SNAPSHOT, Reason.DB, tools.execute_tool,
                                      _TRIAGE, payload, scoped, engine=engine, evaluation_context=context)
            self.events.append(TriageEvent(Stage.SNAPSHOT_COMPLETE))
            parent = self.operation(Stage.PARENT, Reason.PARENT, ledger.capture, _TRIAGE, snapshot)
            parent_json = parent.model_dump_json()
            target = parent.snapshot.summary.detail_target_queue_id
            detail_required = request.include_recommendations and target is not None
            if detail_required:
                continuation = _Continuation(self.id, parent.source_id, parent_json, target,
                                             self.scope, self.deadline)
                self.proposal(adapter, _DETAIL, target)
                detail = self.operation(Stage.DETAIL, Reason.DETAIL, continuation.consume,
                                        ledger, target, self.deadline, tool_limits.max_result_bytes)
                self.operation(Stage.LINK, Reason.LINK, ledger.capture, _DETAIL, detail,
                               parent_triage_source_id=parent.source_id)
            candidate = self.operation(Stage.COMPOSE, Reason.COMPOSE, build_triage_answer, ledger)
            self.operation(Stage.COMPLETE, Reason.COMPLETE, _check_complete,
                           candidate, parent_json, detail_required)
            if len(candidate.model_dump_json().encode()) > min(
                    MAX_RESPONSE_BYTES, self.limits.max_response_bytes):
                raise TriageSizeError("response exceeds size limit")
            self.operation(Stage.PUBLIC, Reason.PUBLIC, validate_triage_response, candidate)
            self.operation(Stage.PRIVATE, Reason.PRIVATE, ledger.validate, candidate)
        self.deadline.remaining()
        return candidate


def _check_complete(candidate, parent_json, detail_required):
    if (type(candidate) is not TriageAgentResponse or candidate.status != "answered"
            or len(candidate.sources) != (2 if detail_required else 1)
            or type(candidate.sources[0]) is not TriageSource
            or candidate.sources[0].model_dump_json() != parent_json):
        raise _Failure(Reason.COMPLETE)
    if detail_required:
        parent, detail = candidate.sources
        if (type(detail) is not LinkedDetailSource or detail.run_id != parent.run_id
                or detail.parent_triage_source_id != parent.source_id
                or detail.snapshot.assessment.queue_id != parent.snapshot.summary.detail_target_queue_id):
            raise _Failure(Reason.COMPLETE)


def run_triage_agent(
    request: TriageAgentRequest, access: AccessContext, *, engine: Engine,
    evaluation_context: tools.EvaluationContext, limits: RuntimeLimits | None = None,
    trace: TriageRuntimeTrace | None = None,
) -> TriageAgentResponse:
    """Run one trusted offline workflow. The question is inert data; composition is server-owned."""
    run = _Run()
    reason = None
    try:
        if trace is not None and type(trace) is not TriageRuntimeTrace:
            raise _Failure(Reason.CONTEXT)
        result = run.execute(request, access, engine, evaluation_context, limits)
    except _Failure as exc:
        reason = exc.reason
    except TriageSizeError:
        reason = Reason.SIZE
    except TimeoutError:
        reason = Reason.TIME
    except Exception:
        reason = Reason.CONTEXT
    if reason is not None:
        result = build_triage_inability(run.id, code=reason.public_code)
    if type(trace) is TriageRuntimeTrace:
        trace.events = (*run.events, TriageEvent(Stage.FINAL))
        trace.reason = reason
        trace.status = result.status
    return result
