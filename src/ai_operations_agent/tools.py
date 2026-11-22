"""Read-only tools; triage is internal-only and absent from legacy agent capabilities."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ai_operations_agent import queue_services
from ai_operations_agent.clock import get_evaluation_time
from ai_operations_agent.config import get_risk_policy
from ai_operations_agent.read_only import ReadOnlyViolation, read_only_session
from ai_operations_agent.risk import RiskPolicy
from ai_operations_agent.tool_schemas import (
    AccessContext,
    ListQueuesArguments,
    QueueIdentity,
    QueueRecommendationsArguments,
    QueueRecommendationsResult,
)

SUPPORTED_TOOLS = frozenset({"list_queues", "get_queue_recommendations"})
INTERNAL_TOOLS = SUPPORTED_TOOLS | {"get_queue_triage"}
_QUEUE_LIST = TypeAdapter(list[QueueIdentity])


class ToolError(ValueError):
    """Stable sanitized error; input values and database details are never included."""


@dataclass(frozen=True)
class EvaluationContext:
    """Trusted offline replay inputs; never accepted as tool arguments or user settings."""

    evaluated_at: datetime
    policy: RiskPolicy


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate argument field")
        result[key] = value
    return result


def _arguments(value: dict | str, max_bytes: int) -> dict:
    try:
        if type(value) is str:
            payload = value
        elif type(value) is dict:
            payload = json.dumps(value, allow_nan=False, separators=(",", ":"))
        else:
            raise ToolError("invalid tool arguments")
        if len(payload.encode("utf-8")) > max_bytes:
            raise ToolError("tool arguments exceed size limit")
        decoded = json.loads(payload, object_pairs_hook=_unique_object)
        if type(decoded) is not dict:
            raise ToolError("invalid tool arguments")
        return decoded
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ToolError):
            raise
        raise ToolError("invalid tool arguments") from None


def execute_tool(
    name: str, arguments: dict | str, access: AccessContext, *, engine: Engine | None = None,
    evaluation_context: EvaluationContext | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Return a fresh JSON snapshot after the owned business session has closed.

    `access` and the optional engine are supplied exclusively by trusted server code.
    No permissions, clock, policy, SQL, or connection can come from tool arguments.
    """
    if type(name) is str and name == "get_queue_triage":
        return _execute_triage(arguments, access, engine, evaluation_context)
    if type(name) is not str or name not in SUPPORTED_TOOLS:
        raise ToolError("unsupported tool")
    if not isinstance(access, AccessContext):
        raise ToolError("tool access denied")
    if name not in access.allowed_tool_names or not access.allowed_queue_ids:
        raise ToolError("tool access denied")
    selected = access.selected_queue_id
    if selected is not None and selected not in access.allowed_queue_ids:
        raise ToolError("queue unavailable")
    payload = _arguments(arguments, access.limits.max_argument_bytes)
    try:
        args = (
            ListQueuesArguments.model_validate(payload, strict=True)
            if name == "list_queues"
            else QueueRecommendationsArguments.model_validate(payload, strict=True)
        )
    except (ValidationError, ValueError, RecursionError):
        raise ToolError("invalid tool arguments") from None
    if isinstance(args, QueueRecommendationsArguments):
        if args.queue_id not in access.allowed_queue_ids:
            raise ToolError("queue unavailable")
        if selected is not None and args.queue_id != selected:
            raise ToolError("queue unavailable")
    try:
        with read_only_session(
            engine, statement_timeout_ms=access.limits.statement_timeout_ms,
        ) as db:
            if isinstance(args, ListQueuesArguments):
                queues = queue_services.list_queues(
                    db, allowed_queue_ids=access.allowed_queue_ids, selected_queue_id=selected,
                )
                if selected is not None and not queues:
                    raise ToolError("queue unavailable")
                identities = _QUEUE_LIST.validate_python(
                    [{"id": queue.id, "name": queue.name} for queue in queues], strict=True,
                )
                if any(
                    queue.id not in access.allowed_queue_ids
                    or (selected is not None and queue.id != selected)
                    for queue in identities
                ):
                    raise ToolError("invalid tool result")
                result = _QUEUE_LIST.dump_json(identities)
            else:
                context = queue_services.assess_queue(
                    db, args.queue_id,
                    evaluation_context.evaluated_at if evaluation_context else get_evaluation_time(),
                    evaluation_context.policy if evaluation_context else get_risk_policy(),
                )
                if context is None:
                    raise ToolError("queue unavailable")
                canonical = queue_services.recommendations_response(context)
                validated = QueueRecommendationsResult.model_validate_json(
                    canonical.model_dump_json(), strict=True,
                )
                if validated.assessment.queue_id != args.queue_id:
                    raise ToolError("invalid tool result")
                result = validated.model_dump_json().encode("utf-8")
            if len(result) > access.limits.max_result_bytes:
                raise ToolError("tool result exceeds size limit")
        return json.loads(result)
    except SQLAlchemyError:
        raise ToolError("database unavailable") from None
    except ValidationError:
        raise ToolError("invalid tool result") from None


def _execute_triage(arguments, access, engine, evaluation_context):
    # The legacy dispatch path and its defaults above deliberately remain unchanged.
    from ai_operations_agent.queue_triage import TriageError, effective_triage_scope, triage_queues
    from ai_operations_agent.triage_schemas import QueueTriageResult

    try:
        if type(access) is not AccessContext:
            raise ToolError("tool access denied")
        access = AccessContext.model_validate(access.model_dump(), strict=True)
        if "get_queue_triage" not in access.allowed_tool_names:
            raise ToolError("tool access denied")
        scope = effective_triage_scope(access)
    except (ValidationError, TypeError, AttributeError):
        raise ToolError("tool access denied") from None
    except TriageError as exc:
        message = str(exc)
        raise ToolError(message if message in {
            "tool access denied", "queue unavailable", "triage scope exceeds limit",
        } else "tool access denied") from None
    payload = _arguments(arguments, access.limits.max_argument_bytes)
    try:
        ListQueuesArguments.model_validate(payload, strict=True)
    except (ValidationError, ValueError, RecursionError):
        raise ToolError("invalid tool arguments") from None
    try:
        if not isinstance(engine, Engine):
            raise ValueError
        if type(evaluation_context) is not EvaluationContext:
            raise ValueError
        when, policy = evaluation_context.evaluated_at, evaluation_context.policy
        if (type(when) is not datetime or when.utcoffset() != timedelta(0)
                or type(policy) is not RiskPolicy):
            raise ValueError
        policy = RiskPolicy(policy.sla_remaining_percent, policy.unassigned_min_tickets)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise ToolError("invalid triage context") from None
    try:
        # Owns a native read-only snapshot and closes it before serialization/validation.
        result = triage_queues(engine, access, evaluated_at=when, policy=policy)
        validated = QueueTriageResult.model_validate_json(result.model_dump_json(), strict=True)
        if (validated.scope != scope or validated.evaluated_at != when
                or validated.risk_policy.risk_policy() != policy):
            raise ToolError("invalid tool result")
        encoded = json.dumps(validated.model_dump(mode="json"), sort_keys=True,
                             ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > access.limits.max_result_bytes:
            raise ToolError("tool result exceeds size limit")
        return json.loads(encoded)
    except TriageError as exc:
        messages = {
            "queue unavailable": "queue unavailable", "database unavailable": "database unavailable",
            "invalid triage data": "invalid tool result", "invalid triage policy": "invalid triage context",
        }
        raise ToolError(messages.get(str(exc), "invalid tool result")) from None
    except (SQLAlchemyError, ReadOnlyViolation):
        raise ToolError("database unavailable") from None
    except ToolError:
        raise
    except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
        raise ToolError("invalid tool result") from None
