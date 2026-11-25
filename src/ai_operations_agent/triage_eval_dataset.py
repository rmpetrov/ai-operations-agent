"""Frozen Sprint 5C fixture and oracle. No production policy or evidence dependencies."""

from collections import Counter
from datetime import datetime
from hashlib import sha256
from importlib.resources import files
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DATASET = "synthetic-queue-triage-eval-v1"
FIXTURE = "queue-triage-fixture-v1"
DIGEST = "84f511688ef528b08ed86f887c0f53b8019cc441adbe1d32016fb0a021479579"
POPULATION = {"task": 12, "security": 10, "failure": 6, "resource": 4}
TRIAGE = "get_queue_triage"
DETAIL = "get_queue_recommendations"
NAMES = {"triage": TRIAGE, "detail": DETAIL}
Count = Annotated[int, Field(ge=0)]
Step = Literal["triage", "detail"]
Condition = Literal["ordinary", "other", "hidden", "duplicate", "early-detail", "extra",
                    "answer", "bad-link", "omit-detail", "refuse", "raise", "build-fault",
                    "deadline", "one-request", "small-response"]
INJECTED = frozenset({"other", "hidden", "duplicate", "early-detail", "extra", "answer",
                      "bad-link", "omit-detail", "refuse", "raise", "build-fault", "deadline"})


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TicketInput(Contract):
    age_minutes: int
    assigned: bool


class Profile(Contract):
    sla_minutes: Annotated[int, Field(gt=0)]
    online: bool
    tickets: tuple[TicketInput, ...]


class Metrics(Contract):
    active_tickets: Count
    unassigned_tickets: Count
    sla_breached_tickets: Count
    sla_at_risk_tickets: Count
    online_agents: Count
    future_created_active_tickets: Count


class Facts(Contract):
    severity: Literal["none", "low", "medium", "high"]
    metrics: Metrics
    signals: tuple[Literal["SLA_BREACHED", "SLA_AT_RISK", "UNASSIGNED_BACKLOG", "NO_AGENTS_ONLINE"], ...]


class Plan(Contract):
    turns: tuple[Step, ...]
    proposals: tuple[Literal["triage", "detail", "other", "hidden", "duplicate", "early-detail", "extra"], ...]
    accepted: Count
    rejected: Count
    dispatch: tuple[Step, ...]
    business_selects: Count
    checkouts: Count
    snapshots: Count
    captures: Count
    detail_builds: Count


class Expected(Contract):
    status: Literal["answered", "error"]
    public_error: Literal["invalid_evidence", "access_denied", "size_limit", "tool_unavailable"] | None
    reason: str | None
    outcome: Literal["attention_required", "no_triggered_rules", "no_available_queues"] | None
    order: tuple[str, ...]
    ties: tuple[str, ...]
    target: str | None
    recommendations: tuple[str, ...]
    sources: Annotated[int, Field(ge=0, le=2)]
    operations: str


class Scenario(Contract):
    id: str
    category: Literal["task", "security", "failure", "resource"]
    scope: tuple[str, ...]
    selected: str | None
    detail: bool
    grants: tuple[Step, ...]
    condition: Condition
    expected: Expected


class Dataset(Contract):
    dataset: Literal["synthetic-queue-triage-eval-v1"]
    fixture: Literal["queue-triage-fixture-v1"]
    evaluated_at: datetime
    profiles: dict[str, Profile]
    facts: dict[str, Facts]
    queues: dict[str, str | None]
    plans: dict[str, Plan]
    scenarios: tuple[Scenario, ...]

    @model_validator(mode="after")
    def validate_manifest(self):
        if (len({s.id for s in self.scenarios}) != 32 or
                Counter(s.category for s in self.scenarios) != POPULATION):
            raise ValueError("incorrect scenario roster")
        if (set(self.profiles) != set(self.facts)
                or set(self.queues.values()) - {None} != set(self.profiles)):
            raise ValueError("invalid fixture profiles")
        for s in self.scenarios:
            e = s.expected
            keys = {*s.scope, *e.order, *e.ties, s.selected, e.target} - {None}
            if (keys - self.queues.keys() or not s.scope or len(set(s.scope)) != len(s.scope)
                    or e.operations not in self.plans or not set(e.ties) <= set(e.order)):
                raise ValueError("invalid scenario references")
            answered = e.status == "answered"
            if (answered != (e.public_error is None and e.reason is None)
                    or answered != bool(e.sources) or answered != bool(e.outcome)
                    or len(set(e.order)) != len(e.order)):
                raise ValueError("inconsistent expected outcome")
        return self


def load_dataset():
    raw = files("ai_operations_agent").joinpath("evaluation_data/triage_v1.json").read_bytes()
    data = Dataset.model_validate_json(raw)
    # Pins every input, ID, category, injection and expected value, including ordering.
    if sha256(raw).hexdigest() != DIGEST:
        raise ValueError("frozen triage dataset mismatch")
    return data
