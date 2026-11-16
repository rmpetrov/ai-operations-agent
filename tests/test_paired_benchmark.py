"""Population, independent scoring, isolation and public reporting for the paired benchmark."""

import json
import socket
import subprocess
import sys
from contextlib import contextmanager
from copy import deepcopy

import httpx
import pytest

from ai_operations_agent import db, evaluation, evaluation_fixtures, paired_benchmark, tools
from ai_operations_agent.evaluation_dataset import load_dataset
from ai_operations_agent.evaluation_fixtures import business_snapshot


@pytest.fixture(autouse=True)
def no_network_or_ambient_database(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("benchmark attempted network or ambient database access")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(db, "get_engine", blocked)
    monkeypatch.setattr(tools, "get_evaluation_time", blocked)
    monkeypatch.setattr(tools, "get_risk_policy", blocked)


@pytest.fixture(scope="module")
def report():
    return paired_benchmark.run_benchmark()


def test_reproduction_is_byte_stable(report):
    repeated = paired_benchmark.run_benchmark()
    assert repeated == report
    assert json.dumps(repeated, sort_keys=True) == json.dumps(report, sort_keys=True)
    assert paired_benchmark.markdown_summary(repeated) == paired_benchmark.markdown_summary(report)


def test_population_denominators_and_classification(report):
    assert report["scenario_count"] == 26
    assert report["comparable_count"] == 22
    assert report["inapplicable_count"] == 4
    assert report["comparable_ids"] == sorted(paired_benchmark.COMPARABLE)
    assert report["inapplicable_ids"] == [
        "duplicate-request", "fabricated-evidence", "incomplete-recommendations",
        "reordered-recommendations",
    ]
    assert [r["scenario_id"] for r in report["results"]] == sorted(
        s.id for s in load_dataset().scenarios
    )
    for row in report["results"]:
        assert row["model_selected"]["passed"]
        assert row["model_selected"]["composition_strategy"] == "model_selected"
        if row["scenario_id"] in paired_benchmark.INAPPLICABLE:
            assert row["population"] == "legacy_only"
            assert row["server_owned"] is None  # No fabricated PASS, FAIL or defense.
            assert row["server_owned_applicability"] == "not_exercised"
        else:
            assert row["population"] == "paired"
            assert row["server_owned_applicability"] == "comparable"
            assert row["server_owned"]["passed"]
            assert row["server_owned"]["composition_strategy"] == "server_owned"
            assert row["server_owned"]["final_model_selection"] == "not_exercised"
    for strategy in paired_benchmark.STRATEGIES:
        summary = report["comparable_summary"][strategy]
        assert summary["total"]["evaluated"] == summary["total"]["passed"] == 22
        assert summary["total"]["failed"] == 0
        assert summary["total"]["expected_errors"] == 8
        assert summary["total"]["correctly_classified_expected_errors"] == 8
        assert summary["total"]["unexpected_errors"] == 0
        assert {key: value["evaluated"] for key, value in summary["by_category"].items()} == {
            "task_success": 9, "security": 6, "failure": 7,
        }
    assert report["legacy_summary"]["evaluated"] == report["legacy_summary"]["passed"] == 26
    assert report["required_gates_passed"]


@pytest.mark.parametrize("mutation", ["missing", "unexpected", "duplicate", "attack_behavior"])
def test_population_drift_is_fatal(mutation):
    dataset = load_dataset()
    scenarios = list(dataset.scenarios)
    if mutation == "missing":
        scenarios.pop()
    elif mutation == "unexpected":
        scenarios[0] = scenarios[0].model_copy(update={"id": "new-scenario"})
    elif mutation == "duplicate":
        scenarios[0] = scenarios[1]
    else:
        index = next(i for i, s in enumerate(scenarios) if s.id == "duplicate-request")
        scenarios[index] = scenarios[index].model_copy(update={"behavior": "demo"})
    with pytest.raises(ValueError, match="population|classification"):
        paired_benchmark.classify(dataset.model_copy(update={"scenarios": tuple(scenarios)}))


@pytest.mark.parametrize("guard_change", ["removed", "different_reason", "expanded"])
def test_current_evaluation_guard_must_agree(monkeypatch, guard_change):
    original = evaluation.evaluate_scenario

    def changed(case, fixtures, **kwargs):
        if kwargs["composition_strategy"].value == "server_owned":
            if case.id in paired_benchmark.INAPPLICABLE:
                if guard_change == "removed":
                    return {}
                if guard_change == "different_reason":
                    raise ValueError("PRIVATE_SENTINEL")
            elif guard_change == "expanded":
                raise ValueError("additional exclusion")
        return original(case, fixtures, **kwargs)

    monkeypatch.setattr(evaluation, "evaluate_scenario", changed)
    with pytest.raises(ValueError, match="guard|additional exclusion"):
        paired_benchmark._run_isolated()


def test_each_cell_starts_with_equivalent_private_fixtures(monkeypatch):
    original = evaluation_fixtures.synthetic_database
    snapshots, engines = [], []

    @contextmanager
    def fresh():
        with original() as fixtures:
            engines.append(fixtures.engine)
            snapshots.append(business_snapshot(fixtures.engine))
            yield fixtures

    monkeypatch.setattr(evaluation_fixtures, "synthetic_database", fresh)
    result = paired_benchmark._run_isolated()
    assert result["required_gates_passed"]
    assert len(snapshots) == len(set(engines)) == 52  # 48 scored cells, four guard checks.
    assert all(snapshot == snapshots[0] for snapshot in snapshots)


def test_failures_are_kept_and_expected_errors_are_distinct(monkeypatch):
    original = tools.execute_tool

    def incomplete(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        if args[0] == "get_queue_recommendations" and snapshot["recommendations"]:
            snapshot["recommendations"].pop()
        return snapshot

    monkeypatch.setattr(tools, "execute_tool", incomplete)
    result = paired_benchmark._run_isolated()
    assert not result["required_gates_passed"]
    assert len(result["results"]) == 26 and result["comparable_count"] == 22
    for strategy in paired_benchmark.STRATEGIES:
        total = result["comparable_summary"][strategy]["total"]
        assert total["evaluated"] == 22 and total["failed"] > 0
        assert total["passed"] + total["failed"] == 22
        selected = next(r[strategy] for r in result["results"]
                        if r["scenario_id"] == "selected-all-signals")
        assert selected["actual_status"] == "answered" and not selected["passed"]
        assert not selected["checks"]["recommendation_membership_order"]
        timeout = next(r[strategy] for r in result["results"] if r["scenario_id"] == "provider-timeout")
        assert timeout["expected_error"] and timeout["passed"] and not timeout["unexpected_error"]


def test_turns_and_requests_are_separate_and_usage_unknown(report):
    one_turn = {"ambiguous-queue-name", "unknown-queue-name", "invalid-arguments", "invalid-tool",
                "provider-malformed", "provider-refusal", "provider-timeout", "provider-unavailable",
                "unauthorized-call", "wrong-queue", "unsupported-capability"}
    for row in report["results"]:
        name = row["scenario_id"]
        model_turns = (0 if name == "unauthorized-selection" else 3 if name == "named-assessment"
                       else 1 if name in one_turn else 2)
        assert row["model_selected"]["model_turn_count"] == model_turns
        if row["server_owned"] is not None:
            reduced = row["model_selected"]["actual_status"] == "answered" or name == "unsupported-capability"
            assert row["server_owned"]["model_turn_count"] == model_turns - int(reduced)
        for strategy in paired_benchmark.STRATEGIES:
            cell = row[strategy]
            if cell is None:
                continue
            assert cell["provider_request_count"] == 0
            assert cell["tool_request_count"] == len(cell["requested_tools"])
            assert cell["tool_dispatch_count"] == len(cell["dispatched_tools"])
            assert all(value is None for value in cell["usage"].values())
            assert cell["estimated_cost_usd"] is None
            assert cell["operational_writes"] == cell["operational_write_attempts"] == 0
            assert not cell["security_violations"]
    assert report["comparable_summary"]["model_selected"]["total"]["model_turn_count"] == 32
    assert report["comparable_summary"]["server_owned"]["total"]["model_turn_count"] == 21
    assert report["legacy_summary"]["model_turn_count"] == 40


def test_public_report_omits_raw_telemetry_and_unsafe_content(report, monkeypatch):
    original = evaluation.evaluate_scenario

    def tainted(*args, **kwargs):
        result = original(*args, **kwargs)
        result["exception"] = result["snapshot"] = "PRIVATE_SENTINEL"
        for event in result["events"]:
            event["private"] = "PRIVATE_SENTINEL"
        return result

    monkeypatch.setattr(evaluation, "evaluate_scenario", tainted)
    public = paired_benchmark._run_isolated()
    assert public == report
    serialized = json.dumps(public) + paired_benchmark.markdown_summary(public)
    for forbidden in ("PRIVATE_SENTINEL", '"events"', '"snapshot"', '"exception"', "argument_digest",
                      "snapshot_digest", "queue_id", "source_id", "pointer", "eval-v1 all",
                      "Synthetic ticket", "Synthetic operator", "encrypted_content", "api_key"):
        assert forbidden not in serialized


def test_worker_excludes_ambient_credentials_files_and_network(tmp_path, monkeypatch, report):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=PRIVATE_SENTINEL\nDATABASE_URL=PRIVATE_SENTINEL\n")
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "DATABASE_URL", "TEST_DATABASE_URL",
                "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.setenv(key, "PRIVATE_SENTINEL")
    original = subprocess.run
    guard = """
import os, sys
def audit(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.bind'}:
        raise AssertionError('network forbidden')
    if event == 'open' and str(args[0]).endswith(('.env', 'credentials')):
        raise AssertionError('credential file forbidden')
    if event == 'import' and args[0].split('.')[0] == 'openai':
        raise AssertionError('provider import forbidden')
sys.addaudithook(audit)
assert not any(key.startswith('OPENAI') for key in os.environ)
assert os.environ['DATABASE_URL'] == 'sqlite+pysqlite:///:memory:'
"""

    def guarded(command, **kwargs):
        assert kwargs["env"] == {"DATABASE_URL": "sqlite+pysqlite:///:memory:"}
        assert kwargs["cwd"] != str(tmp_path)
        assert command[0] == sys.executable and command[1] == "-I"
        return original([*command[:-1], guard + command[-1]], **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)
    assert paired_benchmark.run_benchmark() == report


def test_cli_writes_only_sanitized_deterministic_reports(tmp_path, monkeypatch, capsys, report):
    monkeypatch.setattr(paired_benchmark, "run_benchmark", lambda: deepcopy(report))
    assert paired_benchmark.main(["--output-dir", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "report.json").read_text()) == report
    assert (tmp_path / "summary.md").read_text() == paired_benchmark.markdown_summary(report)
    assert "NOT EXERCISED" in capsys.readouterr().out
    failed = deepcopy(report)
    failed["required_gates_passed"] = False
    monkeypatch.setattr(paired_benchmark, "run_benchmark", lambda: failed)
    assert paired_benchmark.main(["--output-dir", str(tmp_path)]) == 1


def test_cli_and_worker_fail_closed_without_exception_contents(monkeypatch, capsys, tmp_path):
    def broken(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "PRIVATE_SENTINEL", stderr="PRIVATE_SENTINEL")

    monkeypatch.setattr(subprocess, "run", broken)
    assert paired_benchmark.main(["--output-dir", str(tmp_path)]) == 2
    assert "PRIVATE_SENTINEL" not in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []
    for option in ("--live", "--provider", "--scenario", "--database-url"):
        with pytest.raises(SystemExit):
            paired_benchmark.main([option])
