"""Public, static replay wire contracts. Runtime evidence is passed through intact."""

from collections import Counter
from typing import Annotated, Literal, get_args

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from ai_operations_agent.triage_agent_schemas import TriageAgentResponse
from ai_operations_agent.triage_agent_telemetry import Reason, Stage, ToolLabel, TriageRuntimeTrace
from ai_operations_agent.triage_schemas import InternalValue

Workflow = Literal["triage_only", "triage_with_recommendations"]
WORKFLOWS: tuple[Workflow, ...] = get_args(Workflow)
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", max_length=64)]
Count = Annotated[int, Field(ge=0)]


class RecordingContext(InternalValue):
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", max_length=40)]
    working_tree_dirty: bool
    dataset_sha256: Digest
    fixture_version: Literal["queue-triage-fixture-v1"]
    scenario_id: Literal["combined-incident"]
    evaluated_at: AwareDatetime


class ReplayEntry(InternalValue):
    workflow_id: Workflow
    asset_path: Annotated[str, Field(max_length=110)]
    sha256: Digest

    @model_validator(mode="after")
    def content_addressed(self):
        if self.asset_path != f"{self.workflow_id}-{self.sha256}.json":
            raise ValueError("invalid replay asset name")
        return self


class ReplayCatalog(RecordingContext):
    model_config = ConfigDict(title="OpsPilotCatalog")
    schema_version: Literal["opspilot-catalog-v1"]
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=320)
    scope_label: str = Field(min_length=1, max_length=120)
    workflows: tuple[ReplayEntry, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def complete_selection(self):
        if tuple(entry.workflow_id for entry in self.workflows) != WORKFLOWS:
            raise ValueError("incorrect replay roster")
        return self


class DashboardTotals(InternalValue):
    active_tickets: Count
    sla_breached_tickets: Count
    sla_at_risk_tickets: Count
    unassigned_tickets: Count
    online_agents: Count

    @classmethod
    def from_response(cls, response: TriageAgentResponse):
        if response.status != "answered" or not response.sources[0].snapshot.queues:
            return None
        rows = response.sources[0].snapshot.queues
        return cls(
            **{
                field: sum(getattr(row.assessment.metrics, field) for row in rows)
                for field in cls.model_fields
            }
        )


class RecordedEvent(InternalValue):
    stage: Stage
    tool: ToolLabel | None


class OperationCounts(InternalValue):
    adapter_attempts: Count
    proposals: Count
    accepted: Count
    rejected: Count
    snapshot_dispatch_attempts: Count
    completed_snapshots: Count
    detail_dispatch_attempts: Count
    logical_dispatch_attempts: Count

    @classmethod
    def from_events(cls, events: tuple[RecordedEvent, ...]):
        count = Counter(event.stage for event in events)
        return cls(
            adapter_attempts=count[Stage.MODEL],
            proposals=count[Stage.PROPOSED],
            accepted=count[Stage.ACCEPTED],
            rejected=count[Stage.REJECTED],
            snapshot_dispatch_attempts=count[Stage.SNAPSHOT],
            completed_snapshots=count[Stage.SNAPSHOT_COMPLETE],
            detail_dispatch_attempts=count[Stage.DETAIL],
            logical_dispatch_attempts=count[Stage.SNAPSHOT] + count[Stage.DETAIL],
        )


class ReplayTrace(InternalValue):
    status: Literal["answered", "error"]
    reason: Reason | None
    events: tuple[RecordedEvent, ...] = Field(min_length=1, max_length=32)
    measurement_source: Literal["runtime_trace"]
    operation_counts: OperationCounts

    @classmethod
    def from_trace(cls, trace: TriageRuntimeTrace):
        events = tuple(RecordedEvent(stage=e.stage, tool=e.tool) for e in trace.events)
        return cls(
            status=trace.status,
            reason=trace.reason,
            events=events,
            measurement_source="runtime_trace",
            operation_counts=OperationCounts.from_events(events),
        )

    @model_validator(mode="after")
    def consistent_events(self):
        if (
            self.events[-1].stage != Stage.FINAL
            or self.operation_counts != OperationCounts.from_events(self.events)
            or (self.reason is None) != (self.status == "answered")
        ):
            raise ValueError("inconsistent recorded trace")
        return self


class RecordedRun(RecordingContext):
    model_config = ConfigDict(title="OpsPilotRun")
    schema_version: Literal["opspilot-run-v1"]
    mode: Literal["recorded_replay"]
    workflow_id: Workflow
    response: TriageAgentResponse
    dashboard: DashboardTotals | None
    trace: ReplayTrace

    @model_validator(mode="after")
    def consistent_result(self):
        if self.trace.status != self.response.status:
            raise ValueError("trace and release outcomes differ")
        if self.dashboard != DashboardTotals.from_response(self.response):
            raise ValueError("totals disagree with released snapshot")
        if self.response.status == "answered":
            summary = self.response.sources[0].snapshot.summary
            detail = (
                self.workflow_id == "triage_with_recommendations"
                and summary.detail_target_queue_id is not None
            )
            if (
                summary.evaluated_at != self.evaluated_at
                or len(self.response.sources) != 1 + detail
            ):
                raise ValueError("released answer disagrees with recording context")
        return self
