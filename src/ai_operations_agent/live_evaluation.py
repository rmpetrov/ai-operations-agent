"""Controlled smoke-test preparation. Default: offline plan, no credentials or client.

Live execution still needs separate human approval. This milestone tests only an injected
mock SDK transport. No environment setting alone can switch the command into execution.
Runtime/provider imports are deliberately deferred until after every execution gate.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from time import monotonic, perf_counter
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator

from ai_operations_agent.agent_telemetry import RuntimeTrace, TokenUsage, total_usage
from ai_operations_agent.evaluation_costs import VerifiedPriceTable, estimate_cost
from ai_operations_agent.evaluation_dataset import load_dataset
from ai_operations_agent.provider_budget import MAX_SMOKE_REQUESTS, ProviderRequestBudget
from ai_operations_agent.tool_schemas import StrictValue

SCENARIO_IDS = ("authorized-discovery", "selected-all-signals", "named-assessment")
MODEL_ID = "gpt-6-luna"
OPT_IN_VARIABLE = "AGENT_LIVE_SMOKE_OPT_IN"
OPT_IN_VALUE = "approved-synthetic-smoke-v1"
CONFIRMATION = "RUN gpt-6-luna SYNTHETIC SMOKE"


class SmokeSettings(StrictValue):
    provider_enabled: bool = False
    model_id: Literal["gpt-6-luna"] = MODEL_ID
    # Scoped to this smoke runner; general runtime and offline CLI defaults stay at 10s.
    scenario_timeout_seconds: Literal[10, 20, 30] = 30
    request_timeout_seconds: Annotated[int, Field(ge=1, le=10)] = 10
    max_provider_requests: Annotated[int, Field(ge=1, le=MAX_SMOKE_REQUESTS)] = 9
    max_output_tokens: Annotated[int, Field(ge=16, le=2048)] = 2048

    @field_validator("scenario_timeout_seconds", mode="before")
    @classmethod
    def integer_timeout(cls, value):
        if type(value) is not int:
            raise ValueError("scenario timeout must be an integer")
        return value


class SmokeConsent(StrictValue):
    opted_in: Literal[True]
    confirmation: Literal["RUN gpt-6-luna SYNTHETIC SMOKE"]

    @field_validator("opted_in", mode="before")
    @classmethod
    def boolean_opt_in(cls, value):
        if type(value) is not bool:
            raise ValueError("explicit boolean consent required")
        return value


def smoke_scenarios():
    dataset = load_dataset()
    by_id = {scenario.id: scenario for scenario in dataset.scenarios}
    scenarios = tuple(by_id[id] for id in SCENARIO_IDS)
    if any(s.category != "task_success" or s.expected.status != "answered" for s in scenarios):
        raise ValueError("invalid smoke dataset")
    return dataset, scenarios


def execution_plan(settings: SmokeSettings | None = None) -> dict:
    settings = settings or SmokeSettings()
    settings = SmokeSettings.model_validate_json(settings.model_dump_json(), strict=True)
    dataset, scenarios = smoke_scenarios()
    return {
        "schema_version": "live-smoke-plan-v1", "mode": "offline-dry-run",
        "live_execution_approved_by_this_milestone": False,
        "dataset_version": dataset.version, "fixture_version": dataset.fixture_version,
        "scenarios": [{"scenario_id": s.id, "workflow": s.request.workflow,
                       "authorized_queue_scope": list(s.authorized_queue_scope),
                       "selected_queue": s.selected_queue,
                       "expected_status": s.expected.status,
                       "expected_recommendation_codes": s.expected.recommendation_codes}
                      for s in scenarios],
        "database": "private-in-memory-sqlite-synthetic-only",
        "model_id": settings.model_id, "provider_enabled": False,
        "global_max_provider_requests": settings.max_provider_requests,
        "max_provider_requests_per_scenario": 3,
        "max_output_tokens_per_request": settings.max_output_tokens,
        "global_max_output_tokens": settings.max_provider_requests * settings.max_output_tokens,
        "input_token_budget": None,
        "max_tool_requests_per_scenario": 2, "max_model_turns_per_scenario": 3,
        "scenario_timeout_seconds": settings.scenario_timeout_seconds,
        "request_timeout_seconds": settings.request_timeout_seconds,
        "request_timeout_capped_by_remaining_runtime": True,
        "max_question_bytes": 4096, "max_tool_argument_bytes": 4096,
        "max_tool_result_bytes": 65536, "max_model_turn_bytes": 8192,
        "max_provider_payload_bytes": 196608, "max_conversation_bytes": 196608,
        "max_response_bytes": 196608,
        "store": False, "parallel_tool_calls": False, "sdk_retries": 0,
        "hosted_tools": False, "read_only": True, "advisory_only": True,
        "provider_requests_executed": 0, "usage": TokenUsage().model_dump(),
        "estimated_cost_usd": None,
        "required_live_gates": ["separate human approval", "explicit --live",
                                f"process {OPT_IN_VARIABLE}={OPT_IN_VALUE}",
                                "interactive TTY confirmation", "not pytest or CI",
                                "enabled trusted model configuration", "process API credential"],
        "limitations": ["No live compatibility, latency, quality, or cost has been measured.",
                        "Token ceilings include reasoning output; input tokens and cost are unknown.",
                        "Local budgets are not guaranteed provider billing caps.",
                        "Deadlines are cooperative; HTTP timeouts apply to individual network phases."],
    }


def _automation_context() -> bool:
    return "pytest" in sys.modules or any(
        name in os.environ for name in ("PYTEST_CURRENT_TEST", "CI", "GITHUB_ACTIONS")
    )


def _precondition(settings, consent, client_factory):
    try:
        settings = SmokeSettings.model_validate_json(settings.model_dump_json(), strict=True)
    except (AttributeError, ValidationError, ValueError, TypeError):
        return "invalid_model_settings"
    if not settings.provider_enabled:
        return "provider_disabled"
    try:
        SmokeConsent.model_validate_json(consent.model_dump_json(), strict=True)
    except (AttributeError, ValidationError, ValueError, TypeError):
        return "missing_explicit_consent"
    # The injected factory is a trusted testing seam, never a CLI option. Normal pytest/CI
    # cannot reach the actual SDK factory, even with --live, opt-in, and a credential present.
    if client_factory is None:
        if _automation_context():
            return "automated_live_execution_blocked"
        if os.environ.get(OPT_IN_VARIABLE) != OPT_IN_VALUE:
            return "missing_explicit_consent"
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return "interactive_confirmation_required"
    key = os.environ.get("OPENAI_API_KEY")
    if not key or not key.strip():
        return "missing_api_key"
    return None


def _initial_report(settings, *, mocked):
    dataset, scenarios = smoke_scenarios()
    try:
        safe_limits = SmokeSettings.model_validate_json(settings.model_dump_json(), strict=True)
    except (AttributeError, ValidationError, ValueError, TypeError):
        safe_limits = SmokeSettings()
    return {
        "schema_version": "live-smoke-report-v1", "dataset_version": dataset.version,
        "fixture_version": dataset.fixture_version, "git": None,
        "adapter_type": "mocked-openai-responses" if mocked else "openai-responses",
        "mode": "mocked" if mocked else "live", "model_id": MODEL_ID,
        "scenario_count": len(scenarios), "executed_scenario_count": 0,
        "passed": 0, "failed": 0, "required_gates_passed": False,
        "results": [], "not_run": [], "unscored_attempts": [], "metrics": None,
        "security_violations": [], "security_audit_complete": False, "provider_request_count": 0,
        "model_turn_count": 0, "tool_request_count": 0, "tool_dispatch_count": 0,
        "tool_rejection_count": 0,
        "usage": TokenUsage().model_dump(), "cost": estimate_cost(TokenUsage(), MODEL_ID),
        "elapsed_seconds": 0.0, "stop_reason": None,
        "limits": safe_limits.model_dump(),
        "limitations": ["Synthetic smoke set only; no natural-language workflow routing.",
                        "Mocked transport does not measure live model behavior, latency, or API cost.",
                        "Outcomes may vary with live models; timing, Git metadata and usage can differ.",
                        "Missing usage/pricing is unknown. Local estimates are not provider billing caps.",
                        "Adapter protocol rejections appear as model_unavailable; raw candidates are not runtime requests.",
                        "Cooperative deadlines do not guarantee process cancellation or provider-side cancellation."],
    }


def _not_run(report, scenarios, reason):
    done = {result["scenario_id"] for result in report["results"]}
    done.update(result["scenario_id"] for result in report["unscored_attempts"])
    report["not_run"] = [{"scenario_id": s.id, "workflow": s.request.workflow, "reason": reason}
                         for s in scenarios if s.id not in done]


def _mocked_client_factory(factory):
    """Reject accidentally injected live transports before responses.create can run."""
    def create(**kwargs):
        import httpx
        import openai

        client = factory(**kwargs)
        transport = getattr(client, "_client", None)
        safe = (type(client) is openai.OpenAI and type(transport) is httpx.Client
                and type(transport._transport) is httpx.MockTransport
                and all(value is None or type(value) is httpx.MockTransport
                        for value in transport._mounts.values()))
        if not safe:
            client.close()
            raise ValueError("mocked smoke execution requires SDK MockTransport")
        return client
    return create


def _turn_counts(events):
    return {key: sum(event.stage == stage for event in events) for key, stage in (
        ("model_turn_count", "model_turn"), ("tool_request_count", "tool_requested"),
        ("tool_dispatch_count", "tool_dispatched"), ("tool_rejection_count", "tool_rejected"),
    )}


def run_smoke(settings: SmokeSettings, consent: SmokeConsent | None = None, *,
              client_factory=None, clock=monotonic, prices: VerifiedPriceTable | None = None) -> dict:
    """Trusted Python seam. The CLI alone obtains interactive local consent.

    Tests inject only an official SDK client with MockTransport. No custom transport,
    database, questions, model, or permissions can be selected by command-line input.
    """
    start = perf_counter()
    report = _initial_report(settings, mocked=client_factory is not None)
    _, scenarios = smoke_scenarios()
    reason = _precondition(settings, consent, client_factory)
    if reason is None and prices is not None:
        try:
            prices = VerifiedPriceTable.model_validate_json(prices.model_dump_json(), strict=True)
        except (AttributeError, ValidationError, ValueError, TypeError):
            reason = "invalid_price_table"
    if reason:
        report["stop_reason"] = reason
        _not_run(report, scenarios, reason)
        report["elapsed_seconds"] = perf_counter() - start
        return report
    report["limits"] = settings.model_dump()
    # Imports below may load normal backend configuration, but only explicit private
    # SQLite engines, fixed time/policy, and dataset-owned authorization are ever used.
    traces, attempts = [], []
    budget = ProviderRequestBudget(settings.max_provider_requests)
    try:
        from ai_operations_agent import evaluation
        from ai_operations_agent.agent_schemas import RuntimeLimits
        from ai_operations_agent.evaluation_fixtures import synthetic_database
        from ai_operations_agent.openai_responses import OpenAIResponsesAdapter, ResponsesSettings

        report["git"] = evaluation.git_metadata()
        provider = ResponsesSettings(
            enabled=True, model_id=settings.model_id, timeout_seconds=settings.request_timeout_seconds,
            max_output_tokens=settings.max_output_tokens, max_requests=3,
            max_argument_bytes=4096, max_payload_bytes=196608,
        )
        limits = RuntimeLimits(timeout_seconds=settings.scenario_timeout_seconds,
                               max_model_turns=3, max_tool_requests=2)
        factory = _mocked_client_factory(client_factory) if client_factory is not None else None
        with synthetic_database() as fixtures:
            for scenario in scenarios:
                if budget.exhausted:
                    report["stop_reason"] = "global_request_budget_exhausted"
                    break
                trace = RuntimeTrace()
                traces.append(trace)
                attempt = {"scenario_id": scenario.id, "workflow": scenario.request.workflow}
                attempts.append(attempt)
                adapter = OpenAIResponsesAdapter(provider, client_factory=factory,
                                                 clock=clock, trace=trace, request_budget=budget)
                scenario_start = perf_counter()
                try:
                    result = evaluation.evaluate_scenario(
                        scenario, fixtures, adapter=adapter, trace=trace, limits=limits, clock=clock,
                    )
                finally:
                    attempt["elapsed_seconds"] = perf_counter() - scenario_start
                result.update({"workflow": scenario.request.workflow,
                               "model_id": settings.model_id, **_turn_counts(trace.events)})
                report["results"].append(result)
                if result["security_violations"]:
                    report["stop_reason"] = "security_violation"
                    break
                if budget.blocked or budget.exhausted and not result["passed"]:
                    report["stop_reason"] = "global_request_budget_exhausted"
                    break
                if not result["passed"]:
                    report["stop_reason"] = "scenario_gate_failed"
                    break
        report["metrics"] = evaluation.aggregate(report["results"])
        report["metrics"]["planned_task_completion_rate"] = evaluation.rate(
            [result["passed"] for result in report["results"]]
            + [False] * (len(scenarios) - len(report["results"]))
        )
    except Exception:
        # Includes fixture teardown failures. Preserve observed attempts without exposing
        # exception text, operational snapshots, SDK objects, or encrypted reasoning.
        report["stop_reason"] = "smoke_execution_failed"
    report["provider_request_count"] = budget.used
    report.update(_turn_counts([event for trace in traces for event in trace.events]))
    usage = total_usage([value for trace in traces for value in trace.provider_usage])
    report["usage"] = usage.model_dump()
    report["cost"] = estimate_cost(usage, MODEL_ID, prices)
    report["executed_scenario_count"] = len(attempts)
    scored = {result["scenario_id"] for result in report["results"]}
    report["unscored_attempts"] = [
        {**attempt, "status": "execution_or_scoring_failed",
         "provider_request_count": len(trace.provider_usage),
         **_turn_counts(trace.events),
         "events": [e.model_dump(exclude_none=True) for e in trace.events]}
        for attempt, trace in zip(attempts, traces, strict=True) if attempt["scenario_id"] not in scored
    ]
    report["passed"] = sum(r["passed"] for r in report["results"])
    report["failed"] = sum(not r["passed"] for r in report["results"])
    report["security_violations"] = [
        {"scenario_id": r["scenario_id"], "violation": violation}
        for r in report["results"] for violation in r["security_violations"]
    ]
    report["security_audit_complete"] = (not report["unscored_attempts"]
                                          and report["stop_reason"] != "smoke_execution_failed")
    _not_run(report, scenarios, report["stop_reason"] or "not_executed")
    report["required_gates_passed"] = (len(report["results"]) == len(scenarios)
                                       and all(r["passed"] for r in report["results"])
                                       and report["stop_reason"] is None)
    report["elapsed_seconds"] = perf_counter() - start
    return report


def report_markdown(report):
    lines = ["# Controlled synthetic smoke test", "",
             f"Mode: {report['mode']}; model: {report['model_id']}; dataset: {report['dataset_version']}.",
             f"Required gates: {'PASS' if report['required_gates_passed'] else 'FAIL'}; "
             f"stop reason: {report['stop_reason'] or 'none'}.",
             f"Planned: {report['scenario_count']}; executed: {report['executed_scenario_count']}; "
             f"passed: {report['passed']}; failed: {report['failed']}; "
             f"unscored: {len(report['unscored_attempts'])}; not run: {len(report['not_run'])}.",
             f"Provider requests: {report['provider_request_count']}; tokens: "
             f"{report['usage']['total_tokens'] if report['usage']['total_tokens'] is not None else 'unknown'}; "
             f"estimated USD: {report['cost']['estimated_usd'] if report['cost']['estimated_usd'] is not None else 'unknown'}.",
             f"Elapsed seconds: {report['elapsed_seconds']:.6f}.", "",
             "| Scenario | Workflow | Status | Gate |", "| --- | --- | --- | --- |"]
    lines += [f"| {r['scenario_id']} | {r['workflow']} | {r['status']} | {'PASS' if r['passed'] else 'FAIL'} |"
              for r in report["results"]]
    lines += [f"| {r['scenario_id']} | {r['workflow']} | not run | FAIL |" for r in report["not_run"]]
    lines += [f"| {r['scenario_id']} | {r['workflow']} | {r['status']} | FAIL |"
              for r in report["unscored_attempts"]]
    lines += ["", "Recorded security violations: " + (", ".join(
        f"{r['scenario_id']}: {r['violation']}" for r in report["security_violations"]
    ) or "none"), f"Executed-scenario security audit complete: {report['security_audit_complete']}.", ""]
    lines += [f"- {line}" for line in report["limitations"]]
    return "\n".join(lines) + "\n"


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Reject credential/model/URL/permission arguments without echoing their values.
        self.exit(2, "invalid smoke evaluation options\n")


def _cli_consent():
    if _automation_context():
        return None, "automated_live_execution_blocked"
    if os.environ.get(OPT_IN_VARIABLE) != OPT_IN_VALUE:
        return None, "missing_explicit_consent"
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None, "interactive_confirmation_required"
    try:
        answer = input(f"Separate live approval required. Type {CONFIRMATION}: ")
    except (EOFError, KeyboardInterrupt):
        return None, "missing_explicit_consent"
    if answer != CONFIRMATION:
        return None, "missing_explicit_consent"
    return SmokeConsent(opted_in=True, confirmation=answer), None


def main(argv=None):
    parser = _Parser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Requires separate approval and interactive consent")
    mode.add_argument("--dry-run", action="store_true", help="Offline plan (the default)")
    parser.add_argument("--scenario-timeout-seconds", type=int, choices=(10, 20, 30), default=30)
    parser.add_argument("--output-dir", type=Path, default=Path("evaluation-results/live-smoke"))
    args = parser.parse_args(argv)
    settings = SmokeSettings(provider_enabled=args.live,
                             scenario_timeout_seconds=args.scenario_timeout_seconds)
    if not args.live:
        print(json.dumps(execution_plan(settings), indent=2, allow_nan=False))
        return 0
    consent, reason = _cli_consent()
    if reason:
        report = _initial_report(settings, mocked=False)
        report["stop_reason"] = reason
        _, scenarios = smoke_scenarios()
        _not_run(report, scenarios, reason)
    else:
        report = run_smoke(settings, consent)
    try:
        summary = report_markdown(report)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        (args.output_dir / "summary.md").write_text(summary)
    except (OSError, ValueError, TypeError):
        print("Smoke report could not be written.", file=sys.stderr)
        return 2
    print(summary)
    return 0 if report["required_gates_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
