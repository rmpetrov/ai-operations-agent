"""Mode-specific report metadata, tested without executing the real provider transport."""

import json
import os
from datetime import date
from decimal import Decimal

import pytest
from test_discovery_live_evaluation import discovery
from test_live_evaluation import consent
from test_live_evaluation import (
    no_default_database_or_mutable_runtime_session as no_default_database_or_mutable_runtime_session,
)
from test_openai_responses import no_provider_network as no_provider_network

from ai_operations_agent.agent_telemetry import TokenUsage
from ai_operations_agent.evaluation_costs import VerifiedPriceTable, estimate_cost
from ai_operations_agent.live_evaluation import (
    MODEL_ID,
    SmokeSettings,
    _initial_report,
    execution_plan,
    report_markdown,
    run_smoke,
)


@pytest.mark.parametrize("mocked", [False, True])
def test_report_limitations_match_transport_in_json_and_markdown(mocked):
    # Construct metadata only: False exercises live report wording, never live execution.
    report = _initial_report(SmokeSettings(), mocked=mocked)
    assert report["mode"] == ("mocked" if mocked else "live")
    assert report["scenario_count"] == 1 and report["provider_request_count"] == 0
    for rendered in (json.dumps(report), report_markdown(report)):
        assert ("Mocked transport does not measure live model behavior" in rendered) is mocked
        assert "1 planned synthetic scenario" in rendered
        assert "no reliability estimate" in rendered
        assert "no natural-language workflow routing" in rendered
        assert "not actual billed cost" in rendered
        assert "Cooperative deadlines" in rendered
        if not mocked:
            assert "actual billed API cost is not verified by this report" in rendered
            assert "Live observations apply only to this invocation" in rendered


def test_completed_mocked_discovery_retains_mocked_caveat_and_unknown_billing():
    report, provider = discovery()
    assert report["required_gates_passed"] and len(provider.requests) == 2
    assert report["mode"] == "mocked"
    assert "Mocked transport does not measure live model behavior" in report_markdown(report)
    assert report["cost"]["estimated_usd"] is None
    suite = _initial_report(SmokeSettings(execution_mode="mocked-suite"), mocked=True)
    assert suite["scenario_count"] == 3
    assert "3 planned synthetic scenarios" in report_markdown(suite)


def test_blocked_real_transport_report_has_live_limitations_without_reading_credentials(monkeypatch):
    original_get = os.environ.get

    def guarded_get(name, *args):
        assert name != "OPENAI_API_KEY", "blocked report accessed provider credential"
        return original_get(name, *args)

    monkeypatch.setattr(os.environ, "get", guarded_get)
    report = run_smoke(SmokeSettings(provider_enabled=True), consent())
    assert report["stop_reason"] == "automated_live_execution_blocked"
    assert report["mode"] == "live" and report["terminal_outcome"] == "not_attempted"
    assert report["provider_request_count"] == report["executed_scenario_count"] == 0
    assert report["cost"]["estimated_usd"] is None
    assert "Mocked transport" not in report_markdown(report)
    assert "actual billed API cost is not verified" in report_markdown(report)


@pytest.mark.parametrize("mocked", [False, True])
@pytest.mark.parametrize("verified_prices", [False, True])
def test_cost_reporting_requires_verified_prices_and_never_claims_billed_cost(mocked, verified_prices):
    # Synthetic usage/prices test rendering only; no transport, credentials or billing calls.
    usage = TokenUsage(input_tokens=22, output_tokens=14, total_tokens=36)
    prices = VerifiedPriceTable(
        version="synthetic-test-prices-only-v1", model_id=MODEL_ID,
        source_url="https://example.invalid/synthetic-price-fixture",
        externally_verified_on=date(2026, 1, 1),
        usd_per_million_input_tokens=Decimal("2"), usd_per_million_output_tokens=Decimal("4"),
    ) if verified_prices else None
    report = _initial_report(SmokeSettings(), mocked=mocked)
    report["usage"] = usage.model_dump()
    report["cost"] = estimate_cost(usage, MODEL_ID, prices)
    amount = report["cost"]["estimated_usd"]
    assert Decimal(amount) == Decimal("0.0001") if verified_prices else amount is None
    assert report["cost"]["pricing_version"] == (prices.version if prices else None)
    assert "Estimate only" in report["cost"]["label"]
    summary = report_markdown(report)
    assert f"estimated USD: {amount if verified_prices else 'unknown'}" in summary
    assert "Cost estimates require verified pricing" in summary
    assert "estimates are not actual billed cost or provider billing caps" in summary
    if not mocked:
        assert "actual billed API cost is not verified by this report" in summary


def test_offline_plan_limitations_describe_this_invocation_without_erasing_live_observations():
    plan = execution_plan()
    assert plan["mode"] == "offline-dry-run" and plan["provider_requests_executed"] == 0
    assert "Offline plan only; this invocation measures no live behavior" in " ".join(plan["limitations"])
    assert "No live compatibility, latency, quality, or cost has been measured" not in json.dumps(plan)
