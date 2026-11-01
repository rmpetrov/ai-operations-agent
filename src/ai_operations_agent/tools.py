"""Exactly two internal read-only tools, with no provider, runtime, CLI, or agent API."""

import json
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ai_operations_agent import queue_services
from ai_operations_agent.clock import get_evaluation_time
from ai_operations_agent.config import get_risk_policy
from ai_operations_agent.read_only import read_only_session
from ai_operations_agent.tool_schemas import (
    AccessContext,
    ListQueuesArguments,
    QueueIdentity,
    QueueRecommendationsArguments,
    QueueRecommendationsResult,
)

SUPPORTED_TOOLS = frozenset({"list_queues", "get_queue_recommendations"})
_QUEUE_LIST = TypeAdapter(list[QueueIdentity])


class ToolError(ValueError):
    """Stable sanitized error; input values and database details are never included."""


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
) -> dict[str, Any] | list[dict[str, Any]]:
    """Return a fresh JSON snapshot after the owned business session has closed.

    `access` and the optional engine are supplied exclusively by trusted server code.
    No permissions, clock, policy, SQL, or connection can come from tool arguments.
    """
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
                    db, args.queue_id, get_evaluation_time(), get_risk_policy(),
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
