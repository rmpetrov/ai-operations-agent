"""Sprint 5C evaluator controls and deliberate counterexamples to independent scoring."""

import json
import os
import re
import subprocess
import sys
from collections import Counter
from copy import deepcopy
from importlib.resources import files

import pytest
from pydantic import ValidationError
from sqlalchemy import update

from ai_operations_agent import queue_triage
from ai_operations_agent import triage_agent_runtime as rt
from ai_operations_agent import triage_eval_dataset as dataset
from ai_operations_agent import triage_evaluation as ev
from ai_operations_agent.models import Ticket
from ai_operations_agent.triage_eval_observation import Observation
from ai_operations_agent.triage_eval_scoring import check_operations, check_response


@pytest.fixture(scope="module")
def data():
    return dataset.load_dataset()


@pytest.fixture(scope="module")
def report():
    return ev.run_evaluation()


def named(data, name):
    return next(s for s in data.scenarios if s.id == name)


def test_population_and_digest(data):
    assert Counter(s.category for s in data.scenarios) == {"task": 12, "security": 10, "failure": 6, "resource": 4}
    assert len({s.id for s in data.scenarios}) == 32


@pytest.mark.parametrize("defect", ["duplicate", "missing", "unknown_id", "category", "unknown_condition",
                                    "extra_field", "unknown_queue", "wrong_type"])
def test_invalid_manifest_fails_closed(monkeypatch, tmp_path, defect):
    manifest = json.loads(files("ai_operations_agent").joinpath("evaluation_data/triage_v1.json").read_bytes())
    cases = manifest["scenarios"]
    if defect == "duplicate":
        cases[-1]["id"] = cases[0]["id"]
    elif defect == "missing":
        cases.pop()
    elif defect == "unknown_id":
        cases[0]["id"] = "replacement"
    elif defect == "category":
        cases[0]["category"] = "resource"
    elif defect == "unknown_condition":
        cases[0]["condition"] = "new-script"
    elif defect == "extra_field":
        cases[0]["script"] = []
    elif defect == "unknown_queue":
        cases[0]["scope"] = ["wrong"]
    else:
        cases[0]["detail"] = 1
    resource = tmp_path / "evaluation_data"
    resource.mkdir()
    (resource / "triage_v1.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(dataset, "files", lambda _: tmp_path)
    with pytest.raises(ValueError):
        dataset.load_dataset()
    if defect != "unknown_id":
        with pytest.raises(ValidationError):
            dataset.Dataset.model_validate_json(json.dumps(manifest))


def test_ordinary_paths(data, report):
    runs = {r["id"]: r for r in report["scenarios"]}
    for case in data.scenarios:
        if case.condition not in dataset.INJECTED:
            row = runs[case.id]
            assert row["passed"] and row["injection_hits"] is None
    assert runs["maximum-scope-38-references"]["references"] == 38
    assert runs["missing-selected-row"]["operations"]["completed_collections"] == 1
    assert runs["missing-selected-row"]["operations"]["returned_snapshots"] == 0


def test_fixed_injections_and_attempts(data, report):
    injected = {s.id for s in data.scenarios if s.condition in dataset.INJECTED}
    assert len(injected) == 12
    assert {r["id"] for r in report["scenarios"] if r["injection_hits"] == 1 and r["passed"]} == injected
    failed_turn = next(r for r in report["scenarios"] if r["id"] == "second-adapter-failure")
    assert failed_turn["operations"]["model_attempts"] == 2 and failed_turn["operations"]["proposals"] == 1


def test_reproduce_and_privacy(report):
    again = ev.run_evaluation()
    assert json.dumps(report) == json.dumps(again)
    assert ev.markdown(report) == ev.markdown(again)
    serialized = json.dumps(report) + ev.markdown(report)
    for token in ("Synthetic calm", "Fixture ticket", "external_id", '"queue_id"', '"source_id"',
                  '"snapshot"', '"scope"', '"pointer"', '"run_id"', '"call_id"', "FROM tickets",
                  "Review these synthetic", "postgresql://", "sk-proj-"):
        assert token not in serialized
    assert not re.search(r'["\s][a-f0-9]{32}["\s]', serialized)
    assert report["provider"] == dict(live_requests=0, usage=None, billed_cost=None, latency=None)
    assert report["blocked_external_access_attempts"] == 0


@pytest.fixture
def measured(data):
    case = named(data, "all-four-recommendations")
    obs = Observation()
    with ev.private_fixture(data) as (engine, ids):
        raw, reason = ev._measure(data, case, engine, ids, obs)
    return case, ids, raw, reason, obs


@pytest.mark.parametrize(("mutation", "gate"), [
    ("reverse_rows", "ranking"), ("flip_tie", "ranking"), ("wrong_target", "target"),
    ("wrong_metric", "facts"), ("missing_detail", "workflow"),
    ("drop_recommendation", "recommendations"), ("swap_recommendations", "recommendations"),
    ("foreign_pointer", "grounding"), ("foreign_queue", "grounding"), ("wrong_value", "grounding"),
    ("wrong_parent", "recommendations"), ("wrong_block", "grounding"),
])
def test_scorer_rejects_incorrect_answer(data, measured, mutation, gate):
    case, ids, raw, reason, obs = measured
    snapshot = raw["sources"][0]["snapshot"]
    child = raw["sources"][1]
    if mutation == "reverse_rows":
        snapshot["queues"].reverse()
    elif mutation == "flip_tie":
        snapshot["queues"][0]["operational_tie"] = True
    elif mutation == "wrong_target":
        snapshot["summary"]["detail_target_queue_id"] = ids["calm"]
    elif mutation == "wrong_metric":
        snapshot["queues"][0]["assessment"]["metrics"]["sla_breached_tickets"] = 0
    elif mutation == "missing_detail":
        raw["sources"].pop()
    elif mutation == "drop_recommendation":
        child["snapshot"]["recommendations"].pop()
    elif mutation == "swap_recommendations":
        child["snapshot"]["recommendations"].reverse()
    elif mutation == "foreign_pointer":
        raw["evidence"][1]["reference"]["pointer"] = "/queues/1"
    elif mutation == "foreign_queue":
        raw["evidence"][1]["queue_id"] = ids["hidden"]
    elif mutation == "wrong_value":
        raw["evidence"][1]["value"] = raw["evidence"][2]["value"]
    elif mutation == "wrong_parent":
        child["parent_triage_source_id"] = "different"
    else:
        raw["explanation"][1]["references"] = raw["explanation"][2]["references"]
    assert not check_response(data, case, ids, raw, reason, obs)[gate]


def test_valid_but_wrong_evidence_passes_runtime_validator_and_fails_oracle(data):
    case = named(data, "all-four-recommendations")
    obs = Observation()
    with ev.private_fixture(data) as (engine, ids):
        # Move an overdue ticket to the SLA deadline. All downstream data and links agree
        # with this alternate world; it is the wrong world for the declared fixture.
        with engine.begin() as db:
            from datetime import timedelta
            db.execute(update(Ticket).where(Ticket.external_id == "all-0").values(
                created_at=data.evaluated_at - timedelta(minutes=30)))
        raw, reason = ev._measure(data, case, engine, ids, obs)
    assert ev.TriageAgentResponse.model_validate_json(json.dumps(raw), strict=True).status == "answered"
    gates = check_response(data, case, ids, raw, reason, obs)
    assert gates["provenance"] and not gates["facts"] and not gates["recommendations"]


@pytest.mark.parametrize("target", ["calm", "hidden"])
def test_observe_wrong_actual_dispatch_despite_correct_proposal(data, monkeypatch, target):
    case = named(data, "all-four-recommendations")
    original = rt._Run.operation
    obs = Observation()
    with ev.private_fixture(data) as (engine, ids):
        def wrong(run, stage, reason, fn, *args, **kw):
            if stage == rt.Stage.DETAIL:
                ledger, _, deadline, size = args
                args = (ledger, ids[target], deadline, size)
            return original(run, stage, reason, fn, *args, **kw)
        monkeypatch.setattr(rt._Run, "operation", wrong)
        ev._measure(data, case, engine, ids, obs)
    assert obs.proposals[-1]["arguments"] == {"queue_id": ids["all"]}
    assert obs.dispatch[-1][1] == {"queue_id": ids[target]}
    assert obs.counts["unauthorized_dispatches"] == 1 and not obs.builds
    gates = check_operations(case, data.plans[case.expected.operations], ids, obs)
    assert not gates["authorization"] and not gates["arguments"] and not gates["actual_detail_target"]


@pytest.mark.parametrize("defect", ["extra_read", "write_attempt", "wrong_builder"])
def test_execution_probes_are_independent_of_trace(data, monkeypatch, defect):
    case = named(data, "all-four-recommendations")
    obs = Observation()
    with ev.private_fixture(data) as (engine, ids):
        if defect == "write_attempt":
            original = queue_triage.collect_triage_metrics
            def attempt(db, *args):
                db.connection().exec_driver_sql("UPDATE tickets SET priority = 'low'")
                return original(db, *args)
            monkeypatch.setattr(queue_triage, "collect_triage_metrics", attempt)
        else:
            consume = rt._Continuation.consume
            def injected(self, ledger, *args):
                if defect == "extra_read":
                    with engine.connect() as connection:
                        connection.exec_driver_sql("SELECT id FROM queues").all()
                else:
                    rt.recommendations_for_assessment(ledger.sources[0].snapshot.queues[1].assessment)
                return consume(self, ledger, *args)
            monkeypatch.setattr(rt._Continuation, "consume", injected)
        raw, _ = ev._measure(data, case, engine, ids, obs)
    checks = check_operations(case, data.plans[case.expected.operations], ids, obs)
    if defect == "extra_read":
        assert raw["status"] == "answered" and obs.counts["driver_post_snapshot_sql"] > 0
        assert not checks["database_accounting"] and not checks["authorization"]
    elif defect == "write_attempt":
        assert obs.counts["write_attempts"] == 1 and obs.counts["operational_writes"] == 0
        assert not checks["read_only"] and not checks["database_accounting"]
    else:
        assert raw["status"] == "answered" and checks["arguments"] and not checks["actual_detail_target"]


def test_changed_rows_and_reverted_writes(data, monkeypatch):
    case = named(data, "all-four-recommendations")
    original = rt._Continuation.consume
    obs = Observation()
    with ev.private_fixture(data) as (engine, ids):
        def mutate(self, *args):
            with engine.begin() as conn:
                conn.execute(update(Ticket).where(Ticket.external_id == "all-0").values(priority="low"))
            with engine.connect() as conn:
                conn.execute(update(Ticket).where(Ticket.external_id == "all-1").values(priority="high"))
                conn.rollback()
            return original(self, *args)
        monkeypatch.setattr(rt._Continuation, "consume", mutate)
        ev._measure(data, case, engine, ids, obs)
    assert obs.counts["write_attempts"] == obs.counts["operational_writes"] == 2
    assert obs.counts["row_changes"] == 1


def test_unexercised_injection_cannot_pass(data, monkeypatch):
    case = named(data, "wrong-authorized-target")
    original = ev.fixed_fault
    def skip(condition, ids, obs):
        return original("ordinary", ids, obs)
    monkeypatch.setattr(ev, "fixed_fault", skip)
    row = ev._scenario(data, case)
    assert not row["passed"] and not row["checks"]["injection"] and row["injection_hits"] == 0


def test_partial_work_and_applicable_failures_retained(data, report, monkeypatch):
    case = named(data, "all-four-recommendations")
    original = ev._measure
    def broken(*args):
        original(*args)
        raise RuntimeError("private failure material")
    monkeypatch.setattr(ev, "_measure", broken)
    failed = ev._scenario(data, case)
    assert not failed["passed"] and failed["unexpected_error"]
    assert failed["operations"]["logical_dispatches"] == 2 and failed["operations"]["business_selects"] == 2
    rows = [failed if r["id"] == case.id else deepcopy(r) for r in report["scenarios"]]
    scored = ev._aggregate(data, rows)
    assert scored["metrics"]["ranking_accuracy"] == {"numerator": 12, "denominator": 13}
    assert scored["metrics"]["overall"] == {"numerator": 31, "denominator": 32}
    assert scored["metrics"]["recommendation_completeness_order"] == {"numerator": 5, "denominator": 6}
    assert scored["unexpected_failures"] == [case.id]
    assert "private failure material" not in json.dumps(scored)
    for incomplete in (rows[1:], rows + [rows[0]], list(reversed(rows))):
        with pytest.raises(ValueError, match="roster"):
            ev._aggregate(data, incomplete)


def test_metric_populations(report):
    metrics = report["metrics"]
    assert {k: v["denominator"] for k,v in metrics.items()} == {
        "overall": 32, "task_completion": 12, "ranking_accuracy": 13, "target_selection": 13,
        "tool_selection": 32, "argument_correctness": 32, "evidence_grounding": 13,
        "workflow_completeness": 32, "recommendation_completeness_order": 6, "security_pass": 10,
        "expected_failure_handling": 19, "resource_pass": 4, "final_model_selection_quality": 0,
        "proposed_tool_selection": 40, "proposed_arguments": 40,
    }
    assert metrics["final_model_selection_quality"]["numerator"] is None
    assert metrics["proposed_tool_selection"]["numerator"] == 39
    assert metrics["proposed_arguments"]["numerator"] == 36


def test_failed_cli_writes_a_report_and_returns_nonzero(tmp_path, report, monkeypatch):
    failed = deepcopy(report)
    failed["passed"] = False
    monkeypatch.setattr(ev, "run_evaluation", lambda: failed)
    monkeypatch.chdir(tmp_path)
    assert ev.main([]) == 1
    assert json.loads((tmp_path / "evaluation-results/triage/report.json").read_text())["passed"] is False


def test_clean_worker_import_and_execution_without_ambient_access(tmp_path):
    script = """
import os, sys
from pathlib import Path
seen = []
def audit(event, args):
    if (event.startswith('socket.') or (event == 'open' and Path(str(args[0])).name == '.env')
            or (event == 'sqlite3.connect' and args != (':memory:',))):
        seen.append(event)
        raise RuntimeError('forbidden access')
sys.addaudithook(audit)
original = os._Environ.__getitem__
def guard(self, key):
    if key.startswith('OPENAI_') or key in {'DATABASE_URL', 'TEST_DATABASE_URL'}:
        seen.append('configuration')
        raise RuntimeError('forbidden configuration')
    return original(self, key)
os._Environ.__getitem__ = guard
from ai_operations_agent.triage_evaluation import _worker
assert _worker()['passed'] and not seen
assert not {'openai', 'ai_operations_agent.openai_responses', 'ai_operations_agent.config',
            'ai_operations_agent.db'} & sys.modules.keys()
"""
    (tmp_path / ".env").write_text("invalid ambient sentinel")
    subprocess.run([sys.executable, "-I", "-c", script], cwd=tmp_path, check=True,
                   env={**{"PATH": os.environ.get("PATH", "")}, "DATABASE_URL": "forbidden",
                        "TEST_DATABASE_URL": "forbidden", "OPENAI_API_KEY": "forbidden"})
