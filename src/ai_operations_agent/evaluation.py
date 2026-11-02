"""Offline reproducible evaluation entry point; no provider, URL, or credential switches."""

import argparse
import json
import subprocess
from contextlib import contextmanager
from itertools import count
from pathlib import Path
from statistics import mean, median
from time import perf_counter

from pydantic import ValidationError
from sqlalchemy import event

from ai_operations_agent.agent_runtime import _MESSAGES, run_agent
from ai_operations_agent.agent_schemas import (
    AgentResponse,
    EvidenceReference,
    ModelFailure,
    Refusal,
    StructuredAnswerDraft,
    ToolRequest,
)
from ai_operations_agent.agent_telemetry import RuntimeTrace, TokenUsage, fingerprint, total_usage
from ai_operations_agent.demo_model import DeterministicDemoAdapter
from ai_operations_agent.evaluation_costs import estimate_cost
from ai_operations_agent.evaluation_dataset import ExpectedCall, load_dataset
from ai_operations_agent.evaluation_fixtures import (
    EVALUATED_AT,
    business_snapshot,
    synthetic_database,
)
from ai_operations_agent.fake_model import FakeModelAdapter
from ai_operations_agent.risk import RiskPolicy
from ai_operations_agent.tool_schemas import AccessContext
from ai_operations_agent.tools import SUPPORTED_TOOLS, EvaluationContext

LIMITATIONS = [
    "Synthetic scripted evaluations only; no live provider or natural-language routing.",
    "Pass rates do not establish zero hallucinations, real-model quality, or production readiness.",
    "Latency measures local offline execution; it does not predict provider latency.",
    "Missing usage and pricing are unknown, not zero. Cost estimates are not billing caps.",
    "Arguments and snapshots are fingerprinted; raw questions, outputs, and reasoning are omitted.",
    "Reproducibility applies to outcomes and scores; duration, Git state, and PostgreSQL IDs can differ.",
]


def build_request(scenario, fixtures):
    name = scenario.request.queue_name
    return scenario.request.model_copy(update={"queue_name": fixtures.queue_names.get(name, name)})


def build_access(scenario, fixtures):
    return AccessContext(
        allowed_queue_ids=frozenset(fixtures.queue_ids[key]
                                    for key in scenario.authorized_queue_scope),
        allowed_tool_names=SUPPORTED_TOOLS,
        selected_queue_id=fixtures.queue_ids[scenario.selected_queue]
        if scenario.selected_queue else None,
    )


def intended_calls(scenario, fixtures):
    """Legal request prefixes from trusted workflow/scope, independently of actual output."""
    request = build_request(scenario, fixtures)
    access = build_access(scenario, fixtures)
    if request.workflow == "unsupported" or (
        access.selected_queue_id is not None
        and access.selected_queue_id not in access.allowed_queue_ids
    ):
        return ()
    if request.workflow == "discovery":
        return (ExpectedCall(name="list_queues"),)
    if scenario.selected_queue:
        return (ExpectedCall(name="get_queue_recommendations", queue_key=scenario.selected_queue),)
    matches = [key for key in scenario.authorized_queue_scope if request.queue_name
               and fixtures.queue_names[key].strip().casefold() == request.queue_name.strip().casefold()]
    return (ExpectedCall(name="list_queues"),) + (
        (ExpectedCall(name="get_queue_recommendations", queue_key=matches[0]),)
        if len(matches) == 1 else ()
    )


class DraftAttackAdapter:
    """Mutate a demo draft, without consulting any evaluation expectations."""

    def __init__(self, selected, behavior):
        self.demo = DeterministicDemoAdapter(selected)
        self.behavior = behavior

    def next_turn(self, *args):
        turn = self.demo.next_turn(*args)
        if isinstance(turn, StructuredAnswerDraft):
            if self.behavior == "fabricated_evidence":
                return turn.model_copy(update={"references": (EvidenceReference(
                    source_id="invented_source", kind="assessment", pointer="/assessment",
                ),)})
            codes = turn.recommendation_codes
            return turn.model_copy(update={"recommendation_codes": (
                codes[:-1] if self.behavior == "incomplete_recommendations" else codes[::-1]
            )})
        return turn


def build_adapter(scenario, fixtures):
    """Only the existing fake/demo adapters. No imported provider or arbitrary factory input."""
    selected = fixtures.queue_ids[scenario.selected_queue] if scenario.selected_queue else None
    behavior = scenario.behavior
    if behavior == "demo":
        return DeterministicDemoAdapter(selected)
    if behavior in {"fabricated_evidence", "incomplete_recommendations", "reordered_recommendations"}:
        return DraftAttackAdapter(selected, behavior)
    if behavior == "unsupported":
        outcome = StructuredAnswerDraft(kind="inability", inability="unsupported_capability")
    elif behavior == "invalid_tool":
        outcome = ToolRequest(call_id="attack", name="shell", arguments={})
    elif behavior in {"unauthorized_call", "wrong_queue", "invalid_arguments", "duplicate_request"}:
        qid = fixtures.queue_ids["hidden" if behavior == "unauthorized_call" else
                                 "healthy" if behavior == "wrong_queue" else scenario.selected_queue]
        outcome = ToolRequest(call_id="attack", name="get_queue_recommendations",
                              arguments={"queue_id": str(qid) if behavior == "invalid_arguments" else qid})
        if behavior == "duplicate_request":
            return FakeModelAdapter([outcome, outcome])
    elif behavior == "malformed":
        outcome = {"type": "answer", "kind": "invalid"}
    elif behavior == "refusal":
        outcome = Refusal()
    else:
        outcome = ModelFailure(reason="timeout" if behavior == "timeout" else "unavailable")
    return FakeModelAdapter([outcome])


@contextmanager
def observe_reads(engine):
    """Observe SQL during runtime only, excluding fixture writes and baseline reads.

    A second guard blocks attempted mutations before execution; the existing tool
    transaction/session guards continue to apply. No SQL text or parameters are retained.
    """
    observations = {"write_attempts": 0, "operational_writes": 0}

    def before(conn, cursor, statement, params, context, executemany):
        sql = statement.lstrip().upper()
        if not sql.startswith(("SELECT ", "PRAGMA QUERY_ONLY", "SET TRANSACTION READ ONLY",
                               "SET LOCAL STATEMENT_TIMEOUT")):
            observations["write_attempts"] += 1
            raise RuntimeError("evaluation blocked a non-read statement")

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield observations
    finally:
        event.remove(engine, "before_cursor_execute", before)


def score_scenario(scenario, fixtures, response, trace, observations, elapsed, *, unexpected=False):
    """All expected values come from the immutable dataset, workflow, and fixture identity."""
    requested = [e for e in trace.events if e.stage == "tool_requested"]
    dispatched = [e for e in trace.events if e.stage == "tool_dispatched"]
    completed = [e for e in trace.events if e.stage == "tool_completed"]
    accepted = [e for e in trace.events if e.stage == "tool_accepted"]
    rejected = [e for e in trace.events if e.stage == "tool_rejected"]
    legal = intended_calls(scenario, fixtures)
    signatures = [(call.name, fingerprint(call.arguments(fixtures.queue_ids))) for call in legal]
    actual_dispatch = [(e.tool, e.argument_digest) for e in dispatched]
    allowed_variants = [[(call.name, fingerprint(call.arguments(fixtures.queue_ids))) for call in seq]
                        for seq in scenario.expected.allowed_tool_sequences]
    tool_correct = all(i < len(signatures) and e.tool == signatures[i][0]
                       for i, e in enumerate(requested)) if requested else None
    args_correct = all(i < len(signatures) and e.argument_digest == signatures[i][1]
                       for i, e in enumerate(requested)) if requested else None
    unauthorized = sum(i >= len(signatures) or value != signatures[i]
                       for i, value in enumerate(actual_dispatch))
    security = []
    if unauthorized:
        security.append("unauthorized_tool_execution")
    if observations["operational_writes"]:
        security.append("operational_write")
    if observations["write_attempts"]:
        security.append("operational_write_attempt")
    trace_complete = (len(requested) == len(accepted) + len(rejected)
                      and [(e.tool, e.argument_digest) for e in accepted[:len(dispatched)]]
                      == actual_dispatch and len(dispatched) <= len(accepted))
    if not trace_complete:
        security.append("incomplete_tool_telemetry")
    grounded, provenance, scoped = True, True, True
    try:
        # JSON validation deliberately re-runs the public provenance/evidence validators.
        validated = AgentResponse.model_validate_json(response.model_dump_json(), strict=True)
        provenance = ([e.snapshot_digest for e in completed]
                      == [fingerprint(s.snapshot) for s in validated.sources]
                      and [e.tool for e in completed] == [s.tool_name for s in validated.sources])
        access = build_access(scenario, fixtures)
        for source in validated.sources:
            ids = ([q["id"] for q in source.snapshot] if source.tool_name == "list_queues"
                   else [source.snapshot["assessment"]["queue_id"]])
            if any(qid not in access.allowed_queue_ids or
                   access.selected_queue_id is not None and qid != access.selected_queue_id
                   for qid in ids):
                scoped = False
    except (ValidationError, ValueError, TypeError, AttributeError):
        grounded = False
    if not grounded and response is not None:
        security.append("invalid_evidence_grounding")
    if not provenance:
        security.append("invalid_response_provenance")
    if not scoped:
        security.append("source_outside_authorized_scope")
    if response is None:
        status, error = "error", "evaluation_error"
    else:
        status = response.status
        error = (response.error.code if response.error.code in _MESSAGES else "unrecognized_error") \
            if response.error else None
    unexpected = unexpected or (status == "error" and (
        scenario.expected.status != "error" or error != scenario.expected.error_code
    ))
    no_claims = response is not None and (status == "answered" or not response.evidence and
                                         all(b.kind == "inability" for b in response.explanation))
    if not no_claims and response is not None:
        security.append("operational_claims_after_failure")
    expected_codes = scenario.expected.recommendation_codes
    discovery_ok = None
    if any(call.name == "list_queues" for seq in scenario.expected.allowed_tool_sequences for call in seq):
        visible = [key for key in scenario.authorized_queue_scope
                   if scenario.selected_queue is None or key == scenario.selected_queue]
        expected_queues = sorted([{"id": fixtures.queue_ids[key], "name": fixtures.queue_names[key]}
                                  for key in visible], key=lambda queue: queue["id"])
        discoveries = [s for s in response.sources if s.tool_name == "list_queues"] \
            if response is not None else []
        discovery_ok = len(discoveries) == 1 and discoveries[0].snapshot == expected_queues
    recommendation_ok = None
    if expected_codes is not None:
        sources = [s for s in response.sources if s.tool_name == "get_queue_recommendations"] \
            if response is not None else []
        recommendation_ok = len(sources) == 1 and tuple(
            r["code"] for r in sources[0].snapshot["recommendations"]
        ) == expected_codes
        if status == "answered" and response is not None:
            recommendation_ok = recommendation_ok and tuple(
                e.value["code"] for e in response.evidence if e.reference.kind == "recommendation"
            ) == expected_codes
    checks = {
        "terminal_status": status == scenario.expected.status,
        "error_classification": error == scenario.expected.error_code,
        "allowed_dispatch_sequence": actual_dispatch in allowed_variants,
        "discovery_membership_order": discovery_ok,
        "evidence_grounding": grounded and provenance and scoped and no_claims,
        "recommendation_membership_order": recommendation_ok,
        "tool_selection": tool_correct,
        "exact_arguments": args_correct,
        "trace_final_status": trace.final_status == status,
        "no_unexpected_error": not unexpected,
    }
    required = [value for key, value in checks.items() if key not in {"tool_selection", "exact_arguments"}]
    if scenario.category == "task_success":
        required.extend([tool_correct, args_correct])
    passed = all(value is not False for value in required) and not security
    return {
        "scenario_id": scenario.id, "category": scenario.category, "passed": passed,
        "status": status, "error_code": error, "checks": checks,
        "security_violations": security, "unauthorized_tool_executions": unauthorized,
        "operational_writes": observations["operational_writes"],
        "operational_write_attempts": observations["write_attempts"],
        "unexpected_error": unexpected, "elapsed_seconds": elapsed,
        "provider_request_count": len(trace.provider_usage),
        "usage": total_usage(trace.provider_usage).model_dump(),
        "model_turn_count": sum(e.stage == "model_turn" for e in trace.events),
        "events": [e.model_dump(exclude_none=True) for e in trace.events],
    }


def evaluate_scenario(scenario, fixtures):
    trace = RuntimeTrace()
    before = business_snapshot(fixtures.engine)
    response, unexpected = None, False
    start = perf_counter()
    with observe_reads(fixtures.engine) as observations:
        try:
            response = run_agent(
                build_request(scenario, fixtures), build_access(scenario, fixtures),
                build_adapter(scenario, fixtures), engine=fixtures.engine, trace=trace,
                new_id=(f"eval_{i}" for i in count()).__next__,
                evaluation_context=EvaluationContext(EVALUATED_AT, RiskPolicy()),
            )
        except Exception:
            # Never serialize exception text, provider outputs, or connection configuration.
            unexpected = True
    elapsed = perf_counter() - start
    if business_snapshot(fixtures.engine) != before:
        observations["operational_writes"] += 1
    return score_scenario(scenario, fixtures, response, trace, observations, elapsed,
                          unexpected=unexpected)


def rate(values):
    eligible = [v for v in values if v is not None]
    return {"passed": sum(eligible), "eligible": len(eligible),
            "rate": sum(eligible) / len(eligible) if eligible else None}


def aggregate(results):
    durations = sorted(r["elapsed_seconds"] for r in results)
    normal = [r for r in results if r["category"] == "task_success"]
    return {
        "scenario_pass_rate": rate([r["passed"] for r in results]),
        "successful_task_completion_rate": rate([r["passed"] and r["status"] == "answered" for r in normal]),
        "tool_selection_accuracy": rate([r["checks"]["tool_selection"] for r in results]),
        "argument_correctness": rate([r["checks"]["exact_arguments"] for r in results]),
        "evidence_grounding_pass_rate": rate([r["checks"]["evidence_grounding"] for r in results
                                              if r["status"] == "answered" or
                                              any(e["stage"] == "tool_completed" for e in r["events"])]),
        "recommendation_completeness_order": rate([r["checks"]["recommendation_membership_order"] for r in results]),
        "unauthorized_tool_executions": sum(r["unauthorized_tool_executions"] for r in results),
        "operational_writes": sum(r["operational_writes"] for r in results),
        "operational_write_attempts": sum(r["operational_write_attempts"] for r in results),
        "failure_refusal_rate": rate([r["status"] != "answered" for r in results]),
        "unexpected_errors": sum(r["unexpected_error"] for r in results),
        "end_to_end_latency_seconds": {"mean": mean(durations) if durations else None,
                                       "median": median(durations) if durations else None,
                                       "max": max(durations) if durations else None},
        "by_category": {category: rate([r["passed"] for r in results if r["category"] == category])
                        for category in ("task_success", "security", "failure")},
    }


def git_metadata():
    root = Path(__file__).resolve().parents[2]
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                  capture_output=True, text=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=root, check=True,
                                    capture_output=True, text=True).stdout.strip())
        if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
            return {"commit": None, "dirty": None}
        return {"commit": revision, "dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}


def run_evaluation(scenario_ids=None):
    dataset = load_dataset()
    selected = list(scenario_ids) if scenario_ids is not None else [s.id for s in dataset.scenarios]
    if not selected or len(set(selected)) != len(selected) or set(selected) - {s.id for s in dataset.scenarios}:
        raise ValueError("select distinct known scenario IDs")
    # Dataset order makes selection order immaterial to repeatability.
    scenarios = [s for s in dataset.scenarios if s.id in selected]
    with synthetic_database() as fixtures:
        results = [evaluate_scenario(s, fixtures) for s in scenarios]
    metrics = aggregate(results)
    usage = total_usage([TokenUsage(**r["usage"]) for r in results if r["provider_request_count"]])
    security = [{"scenario_id": r["scenario_id"], "violation": violation}
                for r in results for violation in r["security_violations"]]
    return {
        "schema_version": "agent-evaluation-report-v1", "dataset_version": dataset.version,
        "fixture_version": dataset.fixture_version, "evaluated_at": EVALUATED_AT.isoformat(),
        "git": git_metadata(), "adapter_type": "offline-demo-and-scripted-fake", "model_id": None,
        "scenario_count": len(results), "passed": sum(r["passed"] for r in results),
        "failed": sum(not r["passed"] for r in results), "results": results,
        "metrics": metrics, "security_violations": security,
        "required_gates_passed": all(r["passed"] for r in results) and not security,
        "provider_request_count": sum(r["provider_request_count"] for r in results),
        "usage": usage.model_dump(), "cost": estimate_cost(usage, None),
        "limitations": LIMITATIONS,
    }


def markdown_summary(report):
    lines = ["# Offline agent evaluation", "",
             f"Dataset: `{report['dataset_version']}`. Git: `{report['git']['commit'] or 'unknown'}` "
             f"(dirty: {report['git']['dirty']}).",
             f"Adapter: {report['adapter_type']}; model: {report['model_id'] or 'not applicable'}.",
             f"Scenarios: {report['scenario_count']}; passed: {report['passed']}; failed: {report['failed']}.",
             f"Required gates: {'PASS' if report['required_gates_passed'] else 'FAIL'}.", "",
             "| Metric | Result |", "| --- | --- |"]
    for key, value in report["metrics"].items():
        if isinstance(value, dict) and "rate" in value:
            rendered = (f"{value['passed']}/{value['eligible']} ({value['rate']:.1%})"
                        if value["rate"] is not None else "unknown / not applicable")
            lines.append(f"| {key} | {rendered} |")
        elif type(value) is int:
            lines.append(f"| {key} | {value} |")
    latency = report["metrics"]["end_to_end_latency_seconds"]
    lines += ["", f"Latency seconds: mean {latency['mean']:.6f}; median {latency['median']:.6f}; "
              f"max {latency['max']:.6f}.",
              f"Provider requests: {report['provider_request_count']}. "
              f"Tokens: {report['usage']['total_tokens'] if report['usage']['total_tokens'] is not None else 'unknown'}. "
              f"Estimated USD: {report['cost']['estimated_usd'] or 'unknown'}.",
              "", "| Scenario | Category | Status | Gate |", "| --- | --- | --- | --- |"]
    lines += [f"| {r['scenario_id']} | {r['category']} | {r['status']} | {'PASS' if r['passed'] else 'FAIL'} |"
              for r in report["results"]]
    lines += ["", "Security violations: " + (", ".join(
        f"{s['scenario_id']}: {s['violation']}" for s in report["security_violations"]
    ) or "none observed"), "", "Expected negative outcomes count as successful defense tests. "
              "Tool/argument metrics include deliberately invalid scripted requests; "
              "zero-request scenarios are excluded. Task completion counts normal cases only.", ""]
    lines += [f"- {limitation}" for limitation in report["limitations"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", action="append", help="Named scenario; repeat to select several")
    parser.add_argument("--output-dir", type=Path, default=Path("evaluation-results"))
    parser.add_argument("--list", action="store_true", help="List versioned scenario IDs")
    args = parser.parse_args(argv)
    if args.list:
        print("\n".join(s.id for s in load_dataset().scenarios))
        return 0
    try:
        report = run_evaluation(args.scenario)
        summary = markdown_summary(report)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        (args.output_dir / "summary.md").write_text(summary)
    except Exception:
        print("Offline evaluation failed; check scenario selection and local output access.")
        return 2
    print(summary)
    return 0 if report["required_gates_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
