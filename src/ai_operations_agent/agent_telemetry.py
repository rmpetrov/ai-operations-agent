"""Optional request-local metadata. No transcripts, argument values, or provider reasoning."""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field

from ai_operations_agent.answer_diagnostics import RejectionReason
from ai_operations_agent.tool_schemas import StrictValue

ToolLabel = Literal["list_queues", "get_queue_recommendations", "unsupported"]


def fingerprint(value) -> str:
    """Type-sensitive canonical comparison without retaining untrusted values."""
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"),
    ).encode()).hexdigest()


class RuntimeEvent(StrictValue):
    stage: Literal["model_turn", "tool_requested", "tool_accepted", "tool_rejected",
                   "tool_dispatched", "tool_completed"]
    tool: ToolLabel | None = None
    argument_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    snapshot_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class TokenUsage(StrictValue):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


def total_usage(values: list[TokenUsage]) -> TokenUsage:
    # A missing field on any request leaves that aggregate unknown, including failed calls.
    return TokenUsage(**{
        name: sum(parts) if parts and all(part is not None for part in parts) else None
        for name in TokenUsage.model_fields
        for parts in [[getattr(value, name) for value in values]]
    })


@dataclass
class RuntimeTrace:
    """Owned by trusted caller, never included in adapter inputs or public AgentResponse."""

    events: list[RuntimeEvent] = field(default_factory=list)
    provider_usage: list[TokenUsage] = field(default_factory=list)
    final_status: str | None = None
    rejection_reason: RejectionReason | None = None

    def event(self, stage, *, name=None, arguments=None, snapshot=None):
        label = name if name in {"list_queues", "get_queue_recommendations"} else "unsupported"
        self.events.append(RuntimeEvent(
            stage=stage, tool=label if name is not None else None,
            argument_digest=fingerprint(arguments) if arguments is not None else None,
            snapshot_digest=fingerprint(snapshot) if snapshot is not None else None,
        ))

    def model_turn(self, turn):
        self.event("model_turn")
        raw = turn.model_dump() if hasattr(turn, "model_dump") else turn
        if type(raw) is dict and raw.get("type") == "tool_request":
            name = raw.get("name")
            # Only known labels and a digest cross the telemetry boundary.
            name = name if type(name) is str else "unsupported"
            try:
                self.event("tool_requested", name=name, arguments=raw.get("arguments"))
            except (TypeError, ValueError, RecursionError, UnicodeError):
                self.event("tool_requested", name=name)

    def provider_request(self):
        self.provider_usage.append(TokenUsage())

    def provider_response(self, usage):
        if not self.provider_usage or type(usage) is not dict:
            return
        # Explicit allowlist. Booleans, negative counts, extras, and reasoning are ignored.
        self.provider_usage[-1] = TokenUsage(**{
            name: value for name in TokenUsage.model_fields
            if type(value := usage.get(name)) is int and value >= 0
        })
