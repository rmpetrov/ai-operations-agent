"""Local, single-process CLI for the read-only deterministic offline demonstration."""

import argparse
import json
import os
import re
import sys
from uuid import uuid4

from pydantic import ValidationError

from ai_operations_agent.agent_schemas import (
    AgentRequest,
    AgentResponse,
    RuntimeLimits,
    SanitizedError,
)
from ai_operations_agent.tool_schemas import AccessContext

MAX_QUEUE_ID = 2**31 - 1  # PostgreSQL's existing Integer primary key.
MAX_ALLOWED_QUEUES = 32  # The runtime draft supports at most 32 discovery references.
MAX_ALLOWLIST_BYTES = 512
MAX_OUTPUT_BYTES = 196608
SIMULATION_LABEL = "Deterministic offline simulation; question text is not interpreted."


class CLIError(ValueError):
    def __init__(self, code: str, message: str, exit_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


class LocalArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default message can echo arbitrary arguments, including secrets/ANSI.
        raise CLIError("invalid_arguments", "Invalid CLI arguments; use --help.", 2)


def positive_queue_id(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) > MAX_QUEUE_ID:
        raise argparse.ArgumentTypeError("queue ID must be a positive database integer")
    return int(value)


def parse_allowlist(value: str | None) -> frozenset[int]:
    """Only trusted local process configuration grants scope; no wildcards or coercion."""
    try:
        if value is None or len(value.encode("utf-8")) > MAX_ALLOWLIST_BYTES:
            raise ValueError
        parts = value.split(",")
        if not 1 <= len(parts) <= MAX_ALLOWED_QUEUES:
            raise ValueError
        ids = frozenset(positive_queue_id(part.strip()) for part in parts)
        if len(ids) != len(parts):
            raise ValueError
        return ids
    except (ValueError, UnicodeError, argparse.ArgumentTypeError):
        raise CLIError(
            "invalid_scope", "AGENT_ALLOWED_QUEUE_IDS must contain 1-32 distinct positive queue IDs.", 3,
        ) from None


def build_parser() -> argparse.ArgumentParser:
    parser = LocalArgumentParser(
        prog="python -m ai_operations_agent.agent_cli",
        description=SIMULATION_LABEL + " Read-only advisory output; human review required.",
        allow_abbrev=False,
    )

    def common(target, *, default=None):
        # Common options work before or after the command without overwriting earlier values.
        target.add_argument("--question", default=default,
                            help="Bounded demonstration text; never used to select tools or intent.")
        target.add_argument("--json", action="store_true", default=default,
                            help="Emit the existing offline-agent-v1 AgentResponse as JSON.")
        target.add_argument("--evidence", action="store_true", default=default,
                            help="Show successful source IDs, tool names, and validated references.")

    common(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    queues = commands.add_parser("queues", help="List authorized queues.", allow_abbrev=False)
    common(queues, default=argparse.SUPPRESS)
    assess = commands.add_parser("assess", help="Assess one explicit queue.", allow_abbrev=False)
    common(assess, default=argparse.SUPPRESS)
    selection = assess.add_mutually_exclusive_group(required=True)
    selection.add_argument("--queue-id", type=positive_queue_id)
    selection.add_argument("--queue-name", help="Exact name, ignoring case and surrounding whitespace.")
    return parser


def _request_and_access(args) -> tuple[AgentRequest, AccessContext]:
    scope = parse_allowlist(os.environ.get("AGENT_ALLOWED_QUEUE_IDS"))
    selected = getattr(args, "queue_id", None)
    if selected is not None and selected not in scope:
        raise CLIError("queue_unavailable", "queue unavailable", 5)
    try:
        request = AgentRequest(
            question=args.question if args.question is not None else "Demonstrate the selected workflow.",
            workflow="discovery" if args.command == "queues" else "queue_assessment",
            queue_name=getattr(args, "queue_name", None),
        )
        if request.queue_name is not None and not request.queue_name.strip():
            raise ValueError
        serialized = json.dumps(request.model_dump(), ensure_ascii=False, allow_nan=False,
                                separators=(",", ":")).encode("utf-8")
        if len(serialized) > RuntimeLimits().max_question_bytes:
            raise ValueError
    except (ValidationError, ValueError, UnicodeError):
        raise CLIError("invalid_request", "Invalid or oversized demonstration request.", 2) from None
    names = {"list_queues"} if args.command == "queues" else {"get_queue_recommendations"}
    if request.queue_name is not None:
        names.add("list_queues")
    return request, AccessContext(allowed_queue_ids=scope, allowed_tool_names=frozenset(names),
                                  selected_queue_id=selected)


def _run(request: AgentRequest, access: AccessContext) -> AgentResponse:
    # Delay configuration-dependent imports so help/usage and missing settings are safe.
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        from ai_operations_agent.config import settings

        url = make_url(settings.database_url)
        if url.get_backend_name() not in {"sqlite", "postgresql"} or not url.database:
            raise ValueError
    except (ValidationError, ArgumentError, ValueError):
        raise CLIError("invalid_configuration", "Missing or invalid local database/policy configuration.",
                       3) from None

    from ai_operations_agent.agent_runtime import run_agent
    from ai_operations_agent.demo_model import DeterministicDemoAdapter

    return run_agent(request, access, DeterministicDemoAdapter(access.selected_queue_id))


def terminal_text(value: str) -> str:
    """Escape controls, ANSI introducers, bidi controls, and Unicode line separators."""
    return "".join(char if char.isprintable() else (
        f"\\u{ord(char):04x}" if ord(char) <= 0xFFFF else f"\\U{ord(char):08x}"
    ) for char in value)


def _text(response: AgentResponse, evidence: bool) -> str:
    lines = [SIMULATION_LABEL, "Read-only advisory output; human review required."]
    if response.error is not None:
        lines.append(f"Error [{response.error.code}]: {response.error.message}")
    else:
        lines.extend(block.text for block in response.explanation)
        lines.extend(f"Limitation: {item}" for item in response.uncertainty)
    if evidence:
        lines.append("Successful tool sources (not a complete trace of failed or rejected calls):")
        lines.extend(f"  {source.source_id}: {source.tool_name}" for source in response.sources)
        lines.append("Validated references:")
        lines.extend(f"  {item.reference.source_id} {item.reference.kind} {item.reference.pointer} "
                     f"(queue {item.queue_id})" for item in response.evidence)
    return "\n".join(terminal_text(line) for line in lines) + "\n"


def _failure(code: str, message: str) -> AgentResponse:
    return AgentResponse(run_id=uuid4().hex, status="error",
                         error=SanitizedError(code=code, message=message))


def _exit_code(response: AgentResponse) -> int:
    if response.status == "answered":
        return 0
    if response.status in {"needs_clarification", "insufficient_information"}:
        return 5
    if response.error is not None and response.error.code == "queue_unavailable":
        return 5
    return 4


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except CLIError as exc:
        print(f"Error [{exc.code}]: {exc.message}", file=sys.stderr)
        return exc.exit_code
    try:
        request, access = _request_and_access(args)
        response = _run(request, access)
        exit_code = _exit_code(response)
    except CLIError as exc:
        response = _failure(exc.code, exc.message)
        exit_code = exc.exit_code
    except Exception:
        # Import/driver/setup failures must never expose connection strings or exceptions.
        response = _failure("runtime_unavailable", "Local offline runtime unavailable.")
        exit_code = 4

    def render(result):
        # ASCII JSON preserves original values while preventing terminal control sequences.
        return (json.dumps(result.model_dump(mode="json"), ensure_ascii=True, separators=(",", ":"))
                + "\n") if args.json else _text(result, bool(args.evidence))

    output = render(response)
    if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
        output = render(_failure("size_limit", "CLI output exceeds size limit."))
        exit_code = 4
    print(output, end="", file=sys.stdout if args.json or exit_code == 0 else sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
