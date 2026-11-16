"""Paired offline composition benchmark. No live mode or provider configuration."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

BASELINE = "85ad7fb76b48aefaf5761ab0e221cd01327a356e"
# Explicit population contract: dataset drift must fail, never silently change denominators.
COMPARABLE = (
    "adversarial-question", "ambiguous-queue-name", "assess-empty", "assess-healthy",
    "assess-offline", "assess-overdue", "assess-unassigned", "assess-upcoming",
    "authorized-discovery", "invalid-arguments", "invalid-tool", "named-assessment",
    "provider-malformed", "provider-refusal",
    "provider-timeout", "provider-unavailable", "selected-all-signals", "unauthorized-call",
    "unauthorized-selection", "unknown-queue-name", "unsupported-capability", "wrong-queue",
)
INAPPLICABLE = {
    "duplicate-request": "duplicate_request",
    "fabricated-evidence": "fabricated_evidence",
    "incomplete-recommendations": "incomplete_recommendations",
    "reordered-recommendations": "reordered_recommendations",
}
CATEGORIES = ("task_success", "security", "failure")
STRATEGIES = ("model_selected", "server_owned")
CHECKS = (
    "terminal_status", "error_classification", "allowed_dispatch_sequence",
    "discovery_membership_order", "evidence_grounding", "recommendation_membership_order",
    "tool_selection", "exact_arguments", "trace_final_status", "no_unexpected_error",
)
SECURITY_LABELS = frozenset({
    "unauthorized_tool_execution", "operational_write", "operational_write_attempt",
    "incomplete_tool_telemetry", "invalid_evidence_grounding", "invalid_response_provenance",
    "source_outside_authorized_scope", "operational_claims_after_failure",
})
LIMITATIONS = [
    "Deterministic synthetic adapters measure system outcomes, not live model quality.",
    "Model turns are offline adapter turns, not provider requests. Provider requests are zero here.",
    "Official SDK MockTransport request counts are verified separately in tests, not by this runner.",
    "Four legacy final-turn attacks are not exercised or scored under server_owned.",
    "Evidence grounding includes provenance, scope and no claims after failure as one scorer check.",
    "Missing usage and cost remain unknown; no live reliability, latency or billing is measured.",
]


def classify(dataset):
    """Pin the full original cohort and the guard's behavior-to-ID mapping."""
    scenarios = sorted(dataset.scenarios, key=lambda s: s.id)
    expected = set(COMPARABLE) | INAPPLICABLE.keys()
    if len(scenarios) != 26 or {s.id for s in scenarios} != expected:
        raise ValueError("benchmark population differs from the original 26 scenarios")
    attacks = {s.id: s.behavior for s in scenarios if s.behavior in INAPPLICABLE.values()}
    if attacks != INAPPLICABLE:
        raise ValueError("legacy final-turn classification differs from dataset")
    return scenarios


def _public_result(scenario, result):
    """Allowlist fields; raw telemetry, digests, snapshots and exceptions never leave the worker."""
    from ai_operations_agent.agent_runtime import _MESSAGES

    if result["provider_request_count"] != 0 or any(
        value is not None for value in result["usage"].values()
    ):
        raise ValueError("offline benchmark encountered provider accounting")
    events = result["events"]
    tool_names = {"list_queues", "get_queue_recommendations"}
    checks = {key: result["checks"][key] for key in CHECKS}
    if any(value is not None and type(value) is not bool for value in checks.values()):
        raise ValueError("invalid scorer check")
    return {
        "composition_strategy": result["composition_strategy"],
        "expected_status": scenario.expected.status,
        "actual_status": result["status"] if result["status"] in {
            "answered", "unsupported", "error", "needs_clarification", "insufficient_information",
        } else "unrecognized_status",
        "expected_error_code": scenario.expected.error_code,
        "actual_error_code": result["error_code"] if result["error_code"] in _MESSAGES
        or result["error_code"] is None else "unrecognized_error",
        "expected_error": scenario.expected.status == "error",
        "passed": result["passed"],
        "checks": checks,
        "unexpected_error": result["unexpected_error"],
        "model_turn_count": result["model_turn_count"],
        "tool_request_count": sum(e["stage"] == "tool_requested" for e in events),
        "tool_dispatch_count": sum(e["stage"] == "tool_dispatched" for e in events),
        "requested_tools": [e["tool"] if e["tool"] in tool_names else "unsupported"
                            for e in events if e["stage"] == "tool_requested"],
        "dispatched_tools": [e["tool"] if e["tool"] in tool_names else "unsupported"
                             for e in events if e["stage"] == "tool_dispatched"],
        "security_violations": [v if v in SECURITY_LABELS else "unrecognized_violation"
                                for v in result["security_violations"]],
        "unauthorized_tool_executions": result["unauthorized_tool_executions"],
        "operational_writes": result["operational_writes"],
        "operational_write_attempts": result["operational_write_attempts"],
        "final_model_selection": result["final_model_selection"],
        "provider_request_count": 0,
        "usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None},
        "estimated_cost_usd": None,
    }


def _summary(results):
    from ai_operations_agent.evaluation import rate

    return {
        "evaluated": len(results), "passed": sum(r["passed"] for r in results),
        "failed": sum(not r["passed"] for r in results),
        "expected_errors": sum(r["expected_error"] for r in results),
        "correctly_classified_expected_errors": sum(
            r["expected_error"] and r["checks"]["terminal_status"]
            and r["checks"]["error_classification"] for r in results
        ),
        "unexpected_errors": sum(r["unexpected_error"] for r in results),
        "checks": {key: rate([r["checks"][key] for r in results]) for key in CHECKS},
        **{key: sum(r[key] for r in results) for key in (
            "model_turn_count", "tool_request_count", "tool_dispatch_count",
            "provider_request_count", "unauthorized_tool_executions", "operational_writes",
            "operational_write_attempts",
        )},
        "security_violation_count": sum(len(r["security_violations"]) for r in results),
        "final_model_selection_exercised": sum(
            r["final_model_selection"] == "exercised" for r in results
        ),
    }


def _run_isolated():
    """Worker/test seam; production callers use run_benchmark for process isolation."""
    from ai_operations_agent.answer_composition import CompositionStrategy
    from ai_operations_agent.evaluation import evaluate_scenario
    from ai_operations_agent.evaluation_dataset import load_dataset
    from ai_operations_agent.evaluation_fixtures import EVALUATED_AT, synthetic_database

    dataset = load_dataset()
    scenarios = classify(dataset)
    rows = []
    for scenario in scenarios:
        results = {}
        for strategy in CompositionStrategy:
            # Every strategy/scenario starts in a NEW private SQLite database, with a
            # fresh adapter, trace, fixed policy/time, IDs and identical trusted scope.
            with synthetic_database() as fixtures:
                if scenario.id in INAPPLICABLE and strategy is CompositionStrategy.SERVER_OWNED:
                    try:
                        evaluate_scenario(scenario, fixtures, composition_strategy=strategy)
                    except ValueError as exc:
                        if str(exc) != "legacy final-turn attack is not exercised by server composition":
                            raise ValueError("evaluation guard changed") from None
                    else:
                        raise ValueError("evaluation guard no longer excludes final-turn attack")
                    continue
                result = evaluate_scenario(scenario, fixtures, composition_strategy=strategy)
                results[strategy.value] = _public_result(scenario, result)
        rows.append({"scenario_id": scenario.id, "category": scenario.category,
                     "population": "legacy_only" if scenario.id in INAPPLICABLE else "paired",
                     "model_selected": results["model_selected"],
                     "server_owned": results.get("server_owned"),
                     "server_owned_applicability": "not_exercised" if scenario.id in INAPPLICABLE
                     else "comparable"})
    paired = [row for row in rows if row["population"] == "paired"]
    return {
        "schema_version": "paired-composition-benchmark-v1",
        "baseline_commit": BASELINE, "dataset_version": dataset.version,
        "fixture_version": dataset.fixture_version, "evaluated_at": EVALUATED_AT.isoformat(),
        "adapter_type": "offline-demo-and-scripted-fake",
        "scenario_count": len(rows), "comparable_count": len(paired),
        "inapplicable_count": len(rows) - len(paired),
        "comparable_ids": [r["scenario_id"] for r in paired],
        "inapplicable_ids": sorted(INAPPLICABLE), "results": rows,
        "comparable_summary": {strategy: {
            "total": _summary([r[strategy] for r in paired]),
            "by_category": {category: _summary([r[strategy] for r in paired
                                                if r["category"] == category])
                            for category in CATEGORIES},
        } for strategy in STRATEGIES},
        "legacy_summary": _summary([r["model_selected"] for r in rows]),
        "required_gates_passed": all(r[s]["passed"] for r in rows for s in STRATEGIES
                                    if r[s] is not None),
        "limitations": LIMITATIONS,
    }


def run_benchmark():
    """Start without inherited environment, credentials, .env, provider or DB targets.

    The existing evaluation imports application Settings. A clean child cwd/environment
    supplies only a credential-free placeholder SQLite URL, without changing that code
    or the caller's environment. The actual fixtures create their own private engines.
    -I also ignores Python environment overrides and the caller's working directory.
    """
    script = ("import json; from ai_operations_agent.paired_benchmark import _run_isolated; "
              "print(json.dumps(_run_isolated(), sort_keys=True, allow_nan=False))")
    with TemporaryDirectory(prefix="paired-offline-") as directory:
        child = subprocess.run(
            [sys.executable, "-I", "-c", script], cwd=directory,
            env={"DATABASE_URL": "sqlite+pysqlite:///:memory:"},
            capture_output=True, text=True, timeout=120, check=False,
        )
    if child.returncode:
        raise RuntimeError("isolated offline benchmark failed")
    return json.loads(child.stdout)


def markdown_summary(report):
    def status(result):
        return result["actual_status"] + (
            f"/{result['actual_error_code']}" if result["actual_error_code"] else ""
        )

    def gate(result):
        return "PASS" if result["passed"] else "FAIL"

    lines = ["# Paired offline composition benchmark", "",
             f"Dataset: {report['dataset_version']}; comparable: {report['comparable_count']}; "
             f"legacy-only: {report['inapplicable_count']}.", "",
             "M = MODEL_SELECTED; S = SERVER_OWNED. Turns are offline adapter turns.", "",
             "| Scenario | Category | Expected status/error | M actual | S actual | M/S scorer | M/S turns |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in report["results"]:
        m, s = row["model_selected"], row["server_owned"]
        expected = m["expected_status"] + (f"/{m['expected_error_code']}" if m["expected_error_code"] else "")
        lines.append(f"| {row['scenario_id']} | {row['category']} | {expected} | {status(m)} | "
                     f"{status(s) if s else 'NOT EXERCISED'} | {gate(m)}/{gate(s) if s else 'N/A'} | "
                     f"{m['model_turn_count']}/{s['model_turn_count'] if s else 'N/A'} |")
    lines += ["", "| Comparable category | M passed/evaluated | S passed/evaluated | M/S turns |",
              "| --- | --- | --- | --- |"]
    for category in CATEGORIES:
        m, s = (report["comparable_summary"][strategy]["by_category"][category]
                for strategy in STRATEGIES)
        lines.append(f"| {category} | {m['passed']}/{m['evaluated']} | {s['passed']}/{s['evaluated']} | "
                     f"{m['model_turn_count']}/{s['model_turn_count']} |")
    lines += ["", "Expected errors can PASS the independent scorer. Unsuccessful evaluations remain FAIL.",
              "Unexercised server-owned attacks have no score and no pass/fail denominator.",
              "Provider requests: 0. Token usage: unknown. Cost: unknown.", ""]
    lines.extend(f"- {limitation}" for limitation in report["limitations"])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("evaluation-results/paired-composition"))
    args = parser.parse_args(argv)
    try:
        report = run_benchmark()
        summary = markdown_summary(report)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True,
                                                              allow_nan=False) + "\n")
        (args.output_dir / "summary.md").write_text(summary)
    except Exception:
        print("Offline benchmark failed; check the fixed population, installation and output access.")
        return 2
    print(summary)
    return 0 if report["required_gates_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
