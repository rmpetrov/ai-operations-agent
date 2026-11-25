"""Prepare OpsPilot's two synthetic recordings, or export their wire schemas, offline."""

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic.json_schema import GenerateJsonSchema

from ai_operations_agent import triage_agent_runtime
from ai_operations_agent.agent_schemas import RuntimeLimits
from ai_operations_agent.demo_contract import (
    WORKFLOWS,
    DashboardTotals,
    RecordedRun,
    RecordingContext,
    ReplayCatalog,
    ReplayEntry,
    ReplayTrace,
    Workflow,
)
from ai_operations_agent.risk import RiskPolicy
from ai_operations_agent.tool_schemas import AccessContext, ExecutionLimits
from ai_operations_agent.tools import EvaluationContext
from ai_operations_agent.triage_agent_schemas import TriageAgentRequest
from ai_operations_agent.triage_agent_telemetry import TriageRuntimeTrace
from ai_operations_agent.triage_eval_dataset import (
    DETAIL,
    DIGEST,
    TRIAGE,
    Dataset,
    Profile,
    load_dataset,
)
from ai_operations_agent.triage_eval_fixtures import private_fixture
from ai_operations_agent.triage_evidence import validate_triage_response

REPOSITORY = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class _FixtureInputs:
    """Only the seed fields consumed by private_fixture, restricted to this scenario."""

    evaluated_at: datetime
    profiles: dict[str, Profile]
    queues: dict[str, str]


def recording_context(data: Dataset) -> RecordingContext:
    # Read only local revision/status. Never invoke configured fsmonitor or contact a remote.
    def git(*args):
        return subprocess.check_output(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
            cwd=REPOSITORY,
            env={"PATH": os.defpath, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
            text=True,
        ).strip()

    return RecordingContext(
        source_commit=git("rev-parse", "HEAD"),
        working_tree_dirty=bool(git("status", "--porcelain", "--untracked-files=normal")),
        dataset_sha256=DIGEST,
        fixture_version=data.fixture,
        scenario_id="combined-incident",
        evaluated_at=data.evaluated_at,
    )


def record_workflow(workflow: Workflow, data: Dataset, context: RecordingContext) -> RecordedRun:
    if workflow not in WORKFLOWS:
        raise ValueError("unsupported replay workflow")
    detail = workflow == "triage_with_recommendations"
    inputs = _FixtureInputs(
        data.evaluated_at, data.profiles, {k: data.queues[k] for k in ("all", "calm")}
    )
    trace = TriageRuntimeTrace()
    with private_fixture(inputs) as (engine, ids):
        response = triage_agent_runtime.run_triage_agent(
            TriageAgentRequest(
                schema_version="offline-agent-v2",
                workflow="cross_queue_triage",
                question="Review the authorized synthetic queues.",
                include_recommendations=detail,
            ),
            AccessContext(
                allowed_queue_ids=frozenset(ids.values()),
                allowed_tool_names=frozenset((TRIAGE, DETAIL) if detail else (TRIAGE,)),
                limits=ExecutionLimits(),
            ),
            engine=engine,
            evaluation_context=EvaluationContext(data.evaluated_at, RiskPolicy()),
            limits=RuntimeLimits(max_model_turns=2, max_tool_requests=2),
            trace=trace,
        )
    validate_triage_response(response)
    return RecordedRun(
        **context.model_dump(),
        schema_version="opspilot-run-v1",
        mode="recorded_replay",
        workflow_id=workflow,
        response=response,
        dashboard=DashboardTotals.from_response(response),
        trace=ReplayTrace.from_trace(trace),
    )


def export_recordings(directory: Path) -> ReplayCatalog:
    data = load_dataset()  # Verifies the frozen dataset's byte digest.
    context = recording_context(data)
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    with TemporaryDirectory(dir=directory) as staging:
        pending = Path(staging)
        for workflow in WORKFLOWS:
            run = record_workflow(workflow, data, context)
            if run.response.status != "answered":
                raise ValueError("combined incident did not return a complete recording")
            payload = run.model_dump_json().encode("utf-8")
            if len(payload) > 256 * 1024:
                raise ValueError("recording exceeds asset limit")
            digest = sha256(payload).hexdigest()
            entry = ReplayEntry(
                workflow_id=workflow, asset_path=f"{workflow}-{digest}.json", sha256=digest
            )
            (pending / entry.asset_path).write_bytes(payload)
            entries.append(entry)
        catalog = ReplayCatalog(
            **context.model_dump(),
            schema_version="opspilot-catalog-v1",
            title="Combined incident",
            description="Two synthetic queues share a snapshot with SLA pressure, an unassigned backlog and a gap in recorded staffing.",
            scope_label="Synthetic all and Synthetic calm",
            workflows=tuple(entries),
        )
        catalog_bytes = (catalog.model_dump_json(indent=2) + "\n").encode("utf-8")
        if len(catalog_bytes) > 16 * 1024:
            raise ValueError("catalog exceeds asset limit")
        (pending / "catalog.json").write_bytes(catalog_bytes)
        # Publish the manifest last. Prior content-addressed assets remain valid for in-flight readers.
        for entry in entries:
            (pending / entry.asset_path).replace(directory / entry.asset_path)
        (pending / "catalog.json").replace(directory / "catalog.json")
    return catalog


class _WireSchema(GenerateJsonSchema):
    """model_dump_json always emits fields with defaults, including null fields."""

    def field_is_required(self, field, total):
        return True


def export_schemas(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    for name, contract in (("catalog", ReplayCatalog), ("run", RecordedRun)):
        schema = contract.model_json_schema(mode="serialization", schema_generator=_WireSchema)
        (directory / f"{name}.json").write_text(
            json.dumps(schema, indent=2) + "\n", encoding="utf-8"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--output-dir", type=Path, default=REPOSITORY / "frontend/replay-input")
    mode.add_argument("--schema-dir", type=Path, help="Export JSON Schemas only.")
    args = parser.parse_args()
    if args.schema_dir:
        export_schemas(args.schema_dir)
    else:
        catalog = export_recordings(args.output_dir)
        for entry in catalog.workflows:
            print(entry.asset_path)


if __name__ == "__main__":
    main()
