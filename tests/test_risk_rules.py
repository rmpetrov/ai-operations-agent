from dataclasses import replace

import pytest

from ai_operations_agent.risk import RiskMetrics, RiskPolicy, evaluate_risk

EMPTY = RiskMetrics(0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    ("metrics", "codes", "severity"),
    [
        (EMPTY, [], "none"),
        (replace(EMPTY, online_agents=3), [], "none"),
        (RiskMetrics(1, 0, 0, 0, 1, 0), [], "none"),
        (RiskMetrics(1, 1, 0, 0, 1, 0), ["UNASSIGNED_BACKLOG"], "low"),
        (RiskMetrics(1, 0, 0, 0, 0, 0), ["NO_AGENTS_ONLINE"], "medium"),
        (RiskMetrics(1, 0, 0, 1, 1, 0), ["SLA_AT_RISK"], "medium"),
        (RiskMetrics(1, 0, 1, 0, 1, 0), ["SLA_BREACHED"], "high"),
        (
            RiskMetrics(2, 2, 0, 1, 0, 0),
            ["SLA_AT_RISK", "UNASSIGNED_BACKLOG", "NO_AGENTS_ONLINE"],
            "medium",
        ),
        (
            RiskMetrics(3, 2, 1, 1, 0, 1),
            ["SLA_BREACHED", "SLA_AT_RISK", "UNASSIGNED_BACKLOG", "NO_AGENTS_ONLINE"],
            "high",
        ),
    ],
)
def test_rule_order_and_maximum_severity(metrics, codes, severity):
    result = evaluate_risk(metrics, RiskPolicy(), 360)
    assert [signal.code for signal in result.signals] == codes
    assert result.overall_severity == severity
    assert len(codes) == len(set(codes))
    assert evaluate_risk(metrics, RiskPolicy(), 360) == result


@pytest.mark.parametrize(("count", "triggered"), [(0, False), (1, False), (2, True), (3, True)])
def test_unassigned_threshold_is_inclusive(count, triggered):
    metrics = RiskMetrics(3, count, 0, 0, 1, 0)
    signals = evaluate_risk(metrics, RiskPolicy(unassigned_min_tickets=2), 360).signals
    assert bool(signals) is triggered
    if triggered:
        assert signals[0].affected_tickets == count


@pytest.mark.parametrize(
    "kwargs",
    [{"sla_remaining_percent": value} for value in (0, 100, -1, 20.5, True)]
    + [{"unassigned_min_tickets": value} for value in (0, -1, 1.5, True)],
)
def test_policy_rejects_invalid_thresholds(kwargs):
    with pytest.raises(ValueError):
        RiskPolicy(**kwargs)


def test_warning_window_keeps_fractional_seconds():
    assert RiskPolicy(sla_remaining_percent=1).warning_window(1).total_seconds() == 0.6
    assert RiskPolicy().warning_window(1).total_seconds() == 12


@pytest.mark.parametrize(
    ("field", "singular", "plural"),
    [
        (
            "sla_breached_tickets",
            "1 active ticket is past its SLA deadline.",
            "3 active tickets are past their SLA deadline.",
        ),
        (
            "sla_at_risk_tickets",
            "1 active ticket is due now or within 360 seconds.",
            "3 active tickets are due now or within 360 seconds.",
        ),
        (
            "unassigned_tickets",
            "1 active ticket has no assigned agent.",
            "3 active tickets have no assigned agent.",
        ),
        (
            "online_agents",
            "No agents are recorded online for this queue's 1 active ticket.",
            "No agents are recorded online for this queue's 3 active tickets.",
        ),
    ],
)
@pytest.mark.parametrize("count", [1, 3])
def test_signal_explanation_grammar(field, singular, plural, count):
    metrics = replace(EMPTY, active_tickets=count, online_agents=1)
    metrics = replace(metrics, **{field: 0 if field == "online_agents" else count})
    result = evaluate_risk(metrics, RiskPolicy(), 360)
    assert len(result.signals) == 1
    assert result.signals[0].reason == (singular if count == 1 else plural)
