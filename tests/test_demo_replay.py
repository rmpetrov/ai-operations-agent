"""Verify exported public recordings against independent facts and observed runtime execution."""

import json
import subprocess
import sys
from hashlib import sha256

import pytest
from pydantic import ValidationError
from sqlalchemy import event

from ai_operations_agent import demo_replay as replay
from ai_operations_agent import triage_agent_runtime as runtime
from ai_operations_agent.demo_contract import (
    WORKFLOWS,
    DashboardTotals,
    RecordedRun,
    ReplayCatalog,
)
from ai_operations_agent.evaluation_fixtures import business_snapshot
from ai_operations_agent.tool_schemas import AccessContext
from ai_operations_agent.triage_agent_telemetry import Stage

CODES = (
    "REVIEW_OVERDUE_TICKETS",
    "REVIEW_UPCOMING_DEADLINES",
    "VERIFY_RECORDED_AGENT_STATUS",
    "TRIAGE_UNASSIGNED_TICKETS",
)
TOTALS = dict(
    active_tickets=3,
    sla_breached_tickets=1,
    sla_at_risk_tickets=1,
    unassigned_tickets=2,
    online_agents=1,
)


@pytest.fixture(scope="module")
def recordings(tmp_path_factory):
    path = tmp_path_factory.mktemp("combined")
    replay.export_recordings(path)
    catalog = ReplayCatalog.model_validate_json((path / "catalog.json").read_bytes(), strict=True)
    runs = [
        RecordedRun.model_validate_json((path / e.asset_path).read_bytes(), strict=True)
        for e in catalog.workflows
    ]
    return path, catalog, runs


def test_catalog_assets_and_provenance(recordings):
    path, catalog, runs = recordings
    assert (
        catalog.source_commit
        == subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=replay.REPOSITORY, text=True
        ).strip()
    )
    assert catalog.working_tree_dirty == bool(
        subprocess.check_output(
            ["git", "--no-optional-locks", "status", "--porcelain"], cwd=replay.REPOSITORY
        )
    )
    dataset = replay.REPOSITORY / "src/ai_operations_agent/evaluation_data/triage_v1.json"
    assert catalog.dataset_sha256 == sha256(dataset.read_bytes()).hexdigest()
    assert [e.workflow_id for e in catalog.workflows] == list(WORKFLOWS)
    for entry, run in zip(catalog.workflows, runs, strict=True):
        assert entry.asset_path == f"{entry.workflow_id}-{entry.sha256}.json"
        assert sha256((path / entry.asset_path).read_bytes()).hexdigest() == entry.sha256
        assert run.workflow_id == entry.workflow_id
        for field in replay.RecordingContext.model_fields:
            assert getattr(run, field) == getattr(catalog, field)
    assert len({r.response.run_id for r in runs}) == 2
    assert len({s.source_id for r in runs for s in r.response.sources}) == 3


@pytest.mark.parametrize("index", [0, 1])
def test_combined_operational_facts_and_evidence(recordings, index):
    run = recordings[2][index]
    response = run.response
    assert run.evaluated_at.isoformat() == "2026-10-31T16:00:00+00:00"
    assert run.dashboard.model_dump() == TOTALS
    assert response.status == "answered" and response.error is None
    assert response.read_only and response.advisory_only and response.requires_human_review
    assert len(response.sources) == index + 1
    parent = response.sources[0]
    summary, rows = parent.snapshot.summary, parent.snapshot.queues
    assert summary.available_queue_count == 2 and summary.unavailable_queue_count == 0
    assert [(r.position, r.assessment.queue_name) for r in rows] == [
        (1, "Synthetic all"),
        (2, "Synthetic calm"),
    ]
    target = rows[0].assessment.queue_id
    assert summary.detail_target_queue_id == target
    expected = [
        (parent.source_id, "/summary", None),
        (parent.source_id, "/queues/0", target),
        (parent.source_id, "/queues/1", rows[1].assessment.queue_id),
    ]
    if index:
        child = response.sources[1]
        assert child.parent_triage_source_id == parent.source_id
        assert child.snapshot.assessment == rows[0].assessment
        assert tuple(r.code for r in child.snapshot.recommendations) == CODES
        expected.append((child.source_id, "/assessment", target))
        expected.extend((child.source_id, f"/recommendations/{i}", target) for i in range(4))
    actual = [(e.reference.source_id, e.reference.pointer, e.queue_id) for e in response.evidence]
    assert actual == expected
    for item in response.evidence:
        source = next(s for s in response.sources if s.source_id == item.reference.source_id)
        assert source.run_id == response.run_id
        value = source.snapshot.model_dump(mode="json")
        for key in item.reference.pointer[1:].split("/"):
            value = value[int(key) if isinstance(value, list) else key]
        assert value == item.value


def test_request_local_trace_and_database_boundary(tmp_path, monkeypatch):
    run_agent = runtime.run_triage_agent
    dispatch = runtime.tools.execute_tool
    completed, disposed, held_drivers = [], [], []
    post_snapshot = False
    queries, checkouts = [], []

    def track_dispatch(*args, **kwargs):
        nonlocal post_snapshot
        assert args[0] == "get_queue_triage"
        result = dispatch(*args, **kwargs)
        post_snapshot = True
        return result

    def observe(request, access, *, engine, trace, evaluation_context, limits):
        nonlocal post_snapshot
        post_snapshot = False
        assert engine.url.database == ":memory:"
        before = business_snapshot(engine)
        with engine.connect() as connection:
            driver = connection.connection.driver_connection
            held_drivers.append(driver)
            original_changes = driver.total_changes
        assert {row[0] for row in before[0]} == access.allowed_queue_ids
        assert len(before[0]) == 2 and len(before[1]) == 2 and len(before[2]) == 3

        def checkout(*args):
            checkouts.append(post_snapshot)

        def sql(connection, cursor, statement, *args):
            queries.append((post_snapshot, statement.split()[0].upper()))

        event.listen(engine, "checkout", checkout)
        event.listen(engine, "before_cursor_execute", sql)
        event.listen(engine, "engine_disposed", disposed.append)
        try:
            response = run_agent(
                request,
                access,
                engine=engine,
                trace=trace,
                evaluation_context=evaluation_context,
                limits=limits,
            )
        finally:
            event.remove(engine, "before_cursor_execute", sql)
            event.remove(engine, "checkout", checkout)
        assert driver.total_changes == original_changes and before == business_snapshot(engine)
        completed.append((response.model_dump(mode="json"), trace))
        return response

    monkeypatch.setattr(runtime.tools, "execute_tool", track_dispatch)
    monkeypatch.setattr(runtime, "run_triage_agent", observe)
    catalog = replay.export_recordings(tmp_path)
    assert len(disposed) == len(set(disposed)) == len(set(held_drivers)) == 2
    assert checkouts == [False, False]
    assert all(not after for after, _ in queries)
    assert sum(verb == "SELECT" for _, verb in queries) == 4
    assert all(verb not in {"INSERT", "DELETE", "UPDATE"} for _, verb in queries)
    for i, (entry, (response, trace)) in enumerate(zip(catalog.workflows, completed, strict=True)):
        run = RecordedRun.model_validate_json((tmp_path / entry.asset_path).read_bytes())
        assert run.response.model_dump(mode="json") == response
        assert [(e.stage, e.tool) for e in run.trace.events] == [
            (e.stage, e.tool) for e in trace.events
        ]
        assert run.trace.events[-1].stage == Stage.FINAL and run.trace.reason is None
        assert run.trace.measurement_source == "runtime_trace"
        assert run.trace.operation_counts.model_dump() == dict(
            adapter_attempts=1 + i,
            proposals=1 + i,
            accepted=1 + i,
            rejected=0,
            snapshot_dispatch_attempts=1,
            completed_snapshots=1,
            detail_dispatch_attempts=i,
            logical_dispatch_attempts=1 + i,
        )


@pytest.mark.parametrize("missing", [False, True])
def test_source_free_failure_and_empty_snapshot_have_no_totals(monkeypatch, missing):
    original = runtime.run_triage_agent
    data = replay.load_dataset()

    def change_access(request, access, **kwargs):
        # Actual runtime release, either a missing detail grant or an unselected empty scope.
        scope = (
            frozenset({max(access.allowed_queue_ids) + 1}) if missing else access.allowed_queue_ids
        )
        return original(
            request,
            AccessContext(
                allowed_queue_ids=scope, allowed_tool_names=frozenset({"get_queue_triage"})
            ),
            **kwargs,
        )

    monkeypatch.setattr(runtime, "run_triage_agent", change_access)
    run = replay.record_workflow(
        "triage_with_recommendations", data, replay.recording_context(data)
    )
    assert run.dashboard is None
    assert run.trace.status == run.response.status
    if missing:
        assert run.response.sources[0].snapshot.summary.outcome == "no_available_queues"
    else:
        assert run.response.error.code == "access_denied"
        assert run.response.sources == run.response.evidence == ()
    with pytest.raises(ValueError, match="totals disagree"):
        RecordedRun(**{**run.model_dump(), "dashboard": DashboardTotals(**TOTALS)})


def test_disposes_fixture_on_interruption_and_preserves_manifest(tmp_path, monkeypatch):
    catalog = replay.export_recordings(tmp_path)
    prior = (tmp_path / "catalog.json").read_bytes()
    disposed = []
    original = runtime.run_triage_agent

    def interrupt(request, access, *, engine, **kwargs):
        event.listen(engine, "engine_disposed", disposed.append)
        if request.include_recommendations:
            raise RuntimeError("interrupted second recording")
        return original(request, access, engine=engine, **kwargs)

    monkeypatch.setattr(runtime, "run_triage_agent", interrupt)
    with pytest.raises(RuntimeError, match="interrupted second recording"):
        replay.export_recordings(tmp_path)
    assert len(set(disposed)) == 2
    assert (tmp_path / "catalog.json").read_bytes() == prior
    assert {p.name for p in tmp_path.iterdir()} == {
        "catalog.json",
        *(e.asset_path for e in catalog.workflows),
    }


def test_contracts_are_strict_and_require_wire_fields(recordings, tmp_path):
    _, catalog, runs = recordings
    values = runs[0].model_dump(mode="json")
    for field, value in [
        ("extra", 1),
        ("working_tree_dirty", 1),
        ("schema_version", "wrong"),
        ("mode", "local_execution"),
        ("dashboard", None),
        ("workflow_id", "triage_with_recommendations"),
    ]:
        with pytest.raises((ValueError, ValidationError)):
            RecordedRun.model_validate_json(json.dumps({**values, field: value}))
    values["trace"]["operation_counts"]["completed_snapshots"] = 0
    with pytest.raises(ValidationError):
        RecordedRun.model_validate_json(json.dumps(values))
    manifest = catalog.model_dump(mode="json")
    manifest["workflows"][0]["asset_path"] = "../other.json"
    with pytest.raises(ValidationError):
        ReplayCatalog.model_validate_json(json.dumps(manifest))
    replay.export_schemas(tmp_path)
    for name in ("catalog", "run"):
        schema = json.loads((tmp_path / f"{name}.json").read_bytes())
        assert schema["properties"]["schema_version"]["const"] == f"opspilot-{name}-v1"
        for obj in [schema, *schema["$defs"].values()]:
            if "properties" in obj:
                assert set(obj["required"]) == obj["properties"].keys()
                assert obj["additionalProperties"] is False


def test_clean_worker_requires_only_a_private_synthetic_database(tmp_path):
    script = """
import os, sys
from pathlib import Path
attempts = []
def observe(event, args):
    if (event.startswith('socket.') or (event == 'open' and Path(str(args[0])).name == '.env')
            or (event == 'sqlite3.connect' and args != (':memory:',))):
        attempts.append(event)
        raise AssertionError(event)
sys.addaudithook(observe)
get = os._Environ.__getitem__
def guard(self, key):
    if key.startswith('OPENAI_') or key in {'DATABASE_URL', 'TEST_DATABASE_URL'}:
        attempts.append('configuration')
        raise AssertionError('configuration')
    return get(self, key)
os._Environ.__getitem__ = guard
from ai_operations_agent.demo_replay import export_recordings
assert len(export_recordings(Path('out')).workflows) == 2 and attempts == []
assert not {'ai_operations_agent.main', 'ai_operations_agent.db', 'ai_operations_agent.config',
            'ai_operations_agent.seed', 'ai_operations_agent.openai_responses', 'openai'} & sys.modules.keys()
"""
    (tmp_path / ".env").write_text("unused local sentinel")
    subprocess.run([sys.executable, "-I", "-c", script], cwd=tmp_path, env={}, check=True)
