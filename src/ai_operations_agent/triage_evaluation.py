"""Independent offline evaluation of the cross-queue triage workflow."""

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_operations_agent import triage_agent_runtime as runtime
from ai_operations_agent.agent_schemas import RuntimeLimits
from ai_operations_agent.evaluation_fixtures import business_snapshot
from ai_operations_agent.risk import RiskPolicy
from ai_operations_agent.tool_schemas import AccessContext
from ai_operations_agent.tools import EvaluationContext
from ai_operations_agent.triage_agent_schemas import TriageAgentRequest, TriageAgentResponse
from ai_operations_agent.triage_agent_telemetry import TriageRuntimeTrace
from ai_operations_agent.triage_eval_dataset import (
    DIGEST,
    INJECTED,
    NAMES,
    POPULATION,
    load_dataset,
)
from ai_operations_agent.triage_eval_fixtures import fixed_fault, private_fixture
from ai_operations_agent.triage_eval_observation import Observation, effective_ids, observe
from ai_operations_agent.triage_eval_scoring import (
    check_operations,
    check_response,
    operation_counts,
    proposal_quality,
)

REPRODUCE = "python -m ai_operations_agent.triage_evaluation"
LIMITATIONS = (
    "Fixed synthetic SQLite cases exercise a scripted adapter and server-owned answers.",
    "Ranking accuracy means agreement with the declared policy on these fixtures.",
    "The study does not measure LLM reasoning, real model tool selection, intent understanding, "
    "production reliability, provider latency, token use or billing.",
)
METRIC_DEFINITIONS = {
    "overall": "All gates, all scenarios.",
    "task_completion": "All gates on the twelve requested business tasks.",
    "ranking_accuracy": "Exact order, positions and tie markers on expected answers, including empty rankings.",
    "target_selection": "First eligible queue or null, plus the target actually used by the detail builder, on expected answers.",
    "tool_selection": "Accepted and dispatched tool sequence, including required suppression, on every case.",
    "argument_correctness": "Accepted and dispatched arguments against the legal plan on every case.",
    "proposed_tool_selection": "Tool name for the legal stage, per expected or observed proposal, including injected proposals.",
    "proposed_arguments": "Exact argument object for the legal stage, per expected or observed proposal, including injected proposals.",
    "evidence_grounding": "Expected field values, reference sequence, links, values and capture provenance on expected answers.",
    "workflow_completeness": "Expected outcome, source count and operations, including atomic failures, on every case.",
    "recommendation_completeness_order": "All expected recommendation codes, order, linked assessment and evidence on required-detail answers.",
    "security_pass": "All gates on security cases, including exact injection reach when specified.",
    "expected_failure_handling": "All gates on every case expected to return an error, across categories.",
    "resource_pass": "All gates on resource-boundary cases.",
    "final_model_selection_quality": "Inapplicable. The backend composes the answer.",
}


def _measure(data, case, engine, ids, obs):
    grant = AccessContext(allowed_queue_ids=frozenset(ids[k] for k in case.scope),
                          allowed_tool_names=frozenset(NAMES[k] for k in case.grants),
                          selected_queue_id=ids.get(case.selected))
    request = TriageAgentRequest(schema_version="offline-agent-v2", workflow="cross_queue_triage",
                                question="Review these synthetic queues.", include_recommendations=case.detail)
    options = {"one-request": {"max_tool_requests": 1}, "small-response": {"max_response_bytes": 1024}}
    trace = TriageRuntimeTrace()
    scope = effective_ids(case, ids)
    available = scope & {ids[k] for k, profile in data.queues.items() if profile is not None}
    baseline = business_snapshot(engine)
    with engine.connect() as connection:
        driver = connection.connection.driver_connection
        changed = driver.total_changes
    try:
        with fixed_fault(case.condition, ids, obs), observe(engine, driver, case, ids, available, obs):
            result = runtime.run_triage_agent(
                request, grant, engine=engine, evaluation_context=EvaluationContext(data.evaluated_at, RiskPolicy()),
                limits=RuntimeLimits(**options.get(case.condition, {})), trace=trace)
    finally:
        obs.counts["operational_writes"] = driver.total_changes - changed
        for old, new in zip(baseline, business_snapshot(engine), strict=True):
            old_by_id = {r[0]: r for r in old}
            new_by_id = {r[0]: r for r in new}
            obs.counts["row_changes"] += sum(old_by_id.get(k) != new_by_id.get(k)
                                             for k in old_by_id.keys() | new_by_id.keys())
    raw = result.model_dump(mode="json")
    # Also audit the released answer in case a substituted source bypasses the observer.
    released = {item["queue_id"] for item in raw["evidence"] if item["queue_id"] is not None}
    for src in raw["sources"]:
        snap = src["snapshot"]
        if "summary" in snap:
            released.update(snap["summary"]["scope"]["effective_queue_ids"])
            released.update(r["assessment"]["queue_id"] for r in snap["queues"])
        else:
            released.add(snap["assessment"]["queue_id"])
    if released - scope:
        obs.counts["out_of_scope_results"] += 1
    return raw, trace.reason.value if trace.reason else None


def _scenario(data, case):
    obs = Observation()
    checks, status, error, unexpected = {}, None, None, False
    plan = data.plans[case.expected.operations]
    target = references = None
    try:
        with private_fixture(data) as (engine, ids):
            target = ids.get(case.expected.target)
            raw, reason = _measure(data, case, engine, ids, obs)
            # Persist only allowlisted labels even if the runtime returns an unexpected string.
            status = raw["status"] if raw["status"] in {"answered", "error"} else "unexpected"
            if raw["error"] is not None:
                code = raw["error"]["code"]
                error = code if code in {"access_denied", "invalid_evidence", "size_limit", "tool_unavailable"} else "unexpected"
            unexpected = status == "error" and (case.expected.status != status or reason != case.expected.reason
                                                 or error != case.expected.public_error)
            checks.update(check_operations(case, plan, ids, obs))
            checks.update(check_response(data, case, ids, raw, reason, obs))
            checks["database_writes"] = obs.counts["operational_writes"] == 0
            references = len(raw["evidence"])
            # Supplemental check of strict types, safety flags and exact rendered prose.
            # A valid but factually wrong source still fails the independent checks above.
            TriageAgentResponse.model_validate_json(json.dumps(raw), strict=True)
            checks["public_contract"] = True
    except Exception:
        # Preserve this roster slot and all observed attempts, including setup/scoring failures.
        checks["evaluation_completed"] = False
        unexpected = True
    return {"id": case.id, "category": case.category, "expected_status": case.expected.status,
            "expected_error": case.expected.public_error, "actual_status": status, "actual_error": error,
            "passed": bool(checks) and all(checks.values()), "checks": checks,
            "references": references, "proposal_quality": proposal_quality(plan, target, obs),
            "injection_hits": obs.injections if case.condition in INJECTED else None,
            "unexpected_error": unexpected, "operations": operation_counts(obs)}


def _metrics(case, row):
    e, checks = case.expected, row["checks"]
    def ok(*keys):
        return all(checks.get(k, False) for k in keys) and checks.get("evaluation_completed", True)
    success = e.status == "answered"
    return {
        "overall": row["passed"],
        "task_completion": row["passed"] if case.category == "task" else None,
        "ranking_accuracy": ok("ranking") if success else None,
        "target_selection": ok("target", "actual_detail_target") if success else None,
        "tool_selection": ok("tool_selection"),
        "argument_correctness": ok("arguments"),
        "evidence_grounding": ok("grounding", "provenance", "facts", "summary", "ranking", "target",
                                "recommendations", "public_contract") if success else None,
        "workflow_completeness": ok("outcome", "workflow", "execution_accounting", "database_accounting",
                                    "request_accounting", "model_controls", "arguments",
                                    *(("grounding", "recommendations") if success else ("atomic_failure",))),
        "recommendation_completeness_order": ok("recommendations") if e.sources == 2 else None,
        "security_pass": row["passed"] if case.category == "security" else None,
        "expected_failure_handling": row["passed"] if not success else None,
        "resource_pass": row["passed"] if case.category == "resource" else None,
        "final_model_selection_quality": None,
    }


def fraction(values):
    applicable = [v for v in values if v is not None]
    return {"numerator": sum(applicable) if applicable else None, "denominator": len(applicable)}


def _aggregate(data, rows):
    if [r["id"] for r in rows] != [s.id for s in data.scenarios]:
        raise ValueError("evaluation roster mismatch")
    all_ops, answered_ops = Counter(), Counter()
    for case, row in zip(data.scenarios, rows, strict=True):
        row["metrics"] = _metrics(case, row)
        all_ops.update(row["operations"])
        if row["actual_status"] == "answered":
            answered_ops.update(row["operations"])
    metrics = {k: fraction(r["metrics"][k] for r in rows) for k in rows[0]["metrics"]}
    for key in ("proposed_tool_selection", "proposed_arguments"):
        denominator = sum(r["proposal_quality"][key]["denominator"] for r in rows)
        metrics[key] = {"numerator": sum(r["proposal_quality"][key]["numerator"] or 0 for r in rows)
                        if denominator else None, "denominator": denominator}
    security = {
        "unauthorized_dispatches": all_ops["unauthorized_dispatches"],
        "unauthorized_scope_influence": sum(r["operations"]["out_of_scope_results"] > 0 for r in rows),
        "unverified_sql_scope": all_ops["unverified_sql_scope"],
        "operational_writes": all_ops["operational_writes"], "write_attempts": all_ops["write_attempts"],
        "changed_business_rows": all_ops["row_changes"],
        "unexpected_errors": sum(r["unexpected_error"] for r in rows),
        "unexercised_injections": sum(r["injection_hits"] == 0 for r in rows),
    }
    return {"dataset": data.dataset, "fixture": data.fixture, "dataset_sha256": DIGEST,
            "scenario_count": len(rows), "passed": all(r["passed"] for r in rows),
            "categories": {cat: fraction(r["passed"] for r in rows if r["category"] == cat) for cat in POPULATION},
            "metrics": metrics, "metric_definitions": METRIC_DEFINITIONS, "scenarios": rows,
            "all_operations": dict(all_ops), "answered_operations": dict(answered_ops),
            "security_observations": security, "unexpected_failures": [r["id"] for r in rows if not r["passed"]],
            "provider": {"live_requests": 0, "usage": None, "billed_cost": None, "latency": None},
            "limitations": LIMITATIONS, "reproduce": REPRODUCE}


def _worker():
    """Runs only inside a disposable interpreter. Record even caught boundary violations."""
    blocked = Counter()

    def guard(event, args):
        if event.startswith("socket.") or (event == "sqlite3.connect" and args != (":memory:",)):
            blocked["boundary"] += 1
            raise PermissionError("offline fixture boundary")
        if event == "open" and Path(str(args[0])).name == ".env":
            blocked["boundary"] += 1
            raise PermissionError("ambient settings boundary")

    sys.addaudithook(guard)
    data = load_dataset()
    rows = []
    for case in data.scenarios:
        before = blocked["boundary"]
        row = _scenario(data, case)
        row["checks"]["offline_boundary"] = blocked["boundary"] == before
        if not row["checks"]["offline_boundary"]:
            row.update(passed=False, unexpected_error=True)
        rows.append(row)
    report = _aggregate(data, rows)
    report["blocked_external_access_attempts"] = blocked["boundary"]
    report["passed"] &= not blocked["boundary"]
    return report


def run_evaluation():
    """Keep private seam substitutions and environment state isolated from the caller."""
    script = ("import json; from ai_operations_agent.triage_evaluation import _worker; "
              "print(json.dumps(_worker(), allow_nan=False))")
    with TemporaryDirectory(prefix="triage-offline-") as directory:
        child = subprocess.run([sys.executable, "-I", "-c", script], cwd=directory, env={},
                               capture_output=True, text=True, timeout=120, check=False)
    if child.returncode:
        raise RuntimeError("offline evaluation worker failed")
    return json.loads(child.stdout)


def markdown(report):
    def ratio(pair):
        return f"{pair['numerator']}/{pair['denominator']}" if pair["denominator"] else "N/A (0 applicable)"
    lines = ["# Offline cross-queue triage evaluation", "",
             f"Dataset `{report['dataset']}`, fixture `{report['fixture']}`, {report['scenario_count']} scenarios.",
             "", "| Category | Passed / evaluated |", "| --- | ---: |"]
    lines += [f"| {k} | {ratio(v)} |" for k, v in report["categories"].items()]
    lines += ["", "| Metric | Result | Applicable population |", "| --- | ---: | --- |"]
    lines += [f"| {k} | {ratio(v)} | {METRIC_DEFINITIONS[k]} |" for k, v in report["metrics"].items()]
    lines += ["", "| Scenario | Category | Actual outcome | Gate | Model attempts | Proposals | Accepted | Rejected | Dispatches | Returned snapshots | SELECTs | Detail SQL¹ | References | Injection hits |",
              "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in report["scenarios"]:
        c = row["operations"]
        labels = [row["id"], row["category"], row["actual_error"] or row["actual_status"] or "unavailable",
                  "PASS" if row["passed"] else "FAIL"]
        counts = [c[k] for k in ("model_attempts", "proposals", "accepted", "rejected", "logical_dispatches",
                                 "returned_snapshots", "business_selects", "post_snapshot_sql")]
        values = [*labels, *counts, row["references"], row["injection_hits"]]
        lines.append("| " + " | ".join("N/A" if v is None else str(v) for v in values) + " |")
    lines += ["", "¹ Includes every statement after the triage tool returns, through final release.",
              "", "| Operation | All cases | Answered paths |", "| --- | ---: | ---: |"]
    lines += [f"| {k} | {v} | {report['answered_operations'].get(k, 0)} |" for k,v in report["all_operations"].items()]
    lines += ["", "| Security observation | Count |", "| --- | ---: |"]
    lines += [f"| {k} | {v} |" for k,v in report["security_observations"].items()]
    lines += ["", "Failed scenarios " + (", ".join(report["unexpected_failures"]) or "— none."),
              "", *report["limitations"], "", "Live provider requests were zero. Usage, billing and latency are unknown.",
              "", f"Reproduce with `{REPRODUCE}`.", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        report = run_evaluation()
        directory = Path("evaluation-results/triage")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        (directory / "summary.md").write_text(markdown(report))
        print(f"{report['metrics']['overall']['numerator']}/{report['scenario_count']} passed. Reports in {directory}.")
        return 0 if report["passed"] else 1
    except Exception:
        print("Offline triage evaluation failed. Check the frozen dataset, installation and output directory.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
