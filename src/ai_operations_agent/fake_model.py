"""Scripted offline model. Scripts are constructed only by trusted Python callers."""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from copy import deepcopy
from typing import Protocol, runtime_checkable

from ai_operations_agent.agent_schemas import ModelFailure, ModelTurn


class ModelAdapter(Protocol):
    def next_turn(
        self, conversation: tuple[dict, ...], permitted_tools: tuple[str, ...],
        response_schema: dict, deadline: float,
    ) -> ModelTurn:
        """Return one outcome; implementations must honor the monotonic deadline.

        Only detached JSON is passed, never a session, engine, permissions, or ledger.
        """
        ...


@runtime_checkable
class RunScopedModelAdapter(Protocol):
    def open_run(self) -> AbstractContextManager[ModelAdapter]:
        """Optionally give the runtime a private adapter and guaranteed cleanup per run."""
        ...


class FakeModelAdapter:
    def __init__(self, script: Sequence[ModelTurn | Exception]):
        self._script = iter(deepcopy(script))
        self.calls = 0

    def next_turn(self, conversation, permitted_tools, response_schema, deadline) -> ModelTurn:
        self.calls += 1
        outcome = next(self._script, ModelFailure(reason="unavailable"))
        if isinstance(outcome, Exception):
            raise outcome
        return deepcopy(outcome)
