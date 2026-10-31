from dataclasses import replace
from itertools import product

import pytest
from pydantic import TypeAdapter, ValidationError

from ai_operations_agent.recommendations import build_recommendations
from ai_operations_agent.risk import RiskEvaluation, RiskMetrics, RiskPolicy, evaluate_risk
from ai_operations_agent.schemas import ActionRecommendation

CASES = [
    (
        "sla_breached_tickets", "REVIEW_OVERDUE_TICKETS", "SLA_BREACHED", "high",
        "Review overdue active tickets and decide the next response.",
    ),
    (
        "sla_at_risk_tickets", "REVIEW_UPCOMING_DEADLINES", "SLA_AT_RISK", "medium",
        "Review active tickets due now or within the warning window.",
    ),
    (
        "online_agents", "VERIFY_RECORDED_AGENT_STATUS", "NO_AGENTS_ONLINE", "medium",
        "Verify recorded agent status and confirm actual availability.",
    ),
    (
        "unassigned_tickets", "TRIAGE_UNASSIGNED_TICKETS", "UNASSIGNED_BACKLOG", "low",
        "Review unassigned active tickets and decide whether assignment is appropriate.",
    ),
]


@pytest.mark.parametrize(("field", "code", "trigger", "priority", "action"), CASES)
@pytest.mark.parametrize("count", [1, 3])
def test_individual_recommendations_and_exact_wording(field, code, trigger, priority, action, count):
    metrics = RiskMetrics(count, 0, 0, 0, 1, 0)
    metrics = replace(metrics, **{field: 0 if field == "online_agents" else count})
    policy = RiskPolicy()
    evaluation = evaluate_risk(metrics, policy, 360)
    result = build_recommendations(metrics, policy, 360, evaluation)
    assert len(result) == 1
    expected_reason = {
        "sla_breached_tickets": (
            "1 active ticket is past its SLA deadline."
            if count == 1 else "3 active tickets are past their SLA deadline."
        ),
        "sla_at_risk_tickets": (
            f"{count} active {'ticket is' if count == 1 else 'tickets are'} due now or within 360 seconds."
        ),
        "unassigned_tickets": (
            "1 active ticket has no assigned agent."
            if count == 1 else "3 active tickets have no assigned agent."
        ),
        "online_agents": (
            f"No agents are recorded online for this queue's {count} active "
            f"{'ticket' if count == 1 else 'tickets'}. "
            "Recorded status does not establish actual availability."
        ),
    }[field]
    evidence = {
        "sla_breached_tickets": {"sla_breached_tickets": count},
        "sla_at_risk_tickets": {
            "sla_at_risk_tickets": count, "sla_at_risk_window_seconds": 360,
        },
        "unassigned_tickets": {
            "unassigned_tickets": count, "unassigned_backlog_min_tickets": 1,
        },
        "online_agents": {"active_tickets": count, "online_agents": 0},
    }[field]
    assert result[0].model_dump(mode="json") == {
        "code": code, "priority": priority, "triggered_by_signals": [trigger],
        "suggested_action": action, "explanation": expected_reason, "evidence": evidence,
    }
    assert build_recommendations(metrics, policy, 360, evaluation) == result


@pytest.mark.parametrize(("breached", "at_risk", "no_online", "unassigned"), list(product([0, 1], repeat=4)))
def test_all_signal_combinations_and_order(breached, at_risk, no_online, unassigned):
    metrics = RiskMetrics(2, unassigned, breached, at_risk, 1 - no_online, 0)
    policy = RiskPolicy()
    evaluation = evaluate_risk(metrics, policy, 360)
    result = build_recommendations(metrics, policy, 360, evaluation)
    expected = [case[1] for case, enabled in zip(CASES, [breached, at_risk, no_online, unassigned], strict=True) if enabled]
    assert [item.code for item in result] == expected
    assert len(result) == len({item.code for item in result})
    # Catalog ordering and uniqueness must not depend on source order or repeated signals.
    repeated = replace(evaluation, signals=tuple(reversed(evaluation.signals)) * 2)
    assert build_recommendations(metrics, policy, 360, repeated) == result


def test_emitted_signals_are_authoritative():
    metrics = RiskMetrics(4, 3, 1, 1, 0, 0)
    assert build_recommendations(metrics, RiskPolicy(), 360, RiskEvaluation("none", ())) == ()


def test_conflicting_duplicate_signal_rejected():
    metrics = RiskMetrics(1, 0, 1, 0, 1, 0)
    evaluation = evaluate_risk(metrics, RiskPolicy(), 360)
    signal = evaluation.signals[0]
    evaluation = replace(evaluation, signals=(signal, replace(signal, affected_tickets=2)))
    with pytest.raises(ValueError, match="conflicting duplicate"):
        build_recommendations(metrics, RiskPolicy(), 360, evaluation)


def test_nondefault_policy_evidence_and_fractional_window():
    metrics = RiskMetrics(4, 3, 0, 1, 1, 0)
    policy = RiskPolicy(sla_remaining_percent=1, unassigned_min_tickets=3)
    window = policy.warning_window(1).total_seconds()
    result = build_recommendations(metrics, policy, window, evaluate_risk(metrics, policy, window))
    assert result[0].evidence.model_dump() == {
        "sla_at_risk_tickets": 1, "sla_at_risk_window_seconds": 0.6,
    }
    assert result[0].explanation == "1 active ticket is due now or within 0.6 seconds."
    assert result[1].evidence.model_dump() == {
        "unassigned_tickets": 3, "unassigned_backlog_min_tickets": 3,
    }


@pytest.mark.parametrize("case_index", range(4))
@pytest.mark.parametrize("invalid", ["code", "priority", "trigger", "extra", "evidence", "count", "missing", "boolean"])
def test_code_specific_evidence_schema(case_index, invalid):
    metrics = RiskMetrics(2, 2, 1, 1, 0, 0)
    result = build_recommendations(metrics, RiskPolicy(), 360, evaluate_risk(metrics, RiskPolicy(), 360))
    payload = result[case_index].model_dump(mode="json")
    if invalid == "code":
        payload["code"] = "ASSIGN_TICKET"
    elif invalid == "priority":
        payload["priority"] = "low" if payload["priority"] == "high" else "high"
    elif invalid == "trigger":
        payload["triggered_by_signals"] = ["SLA_AT_RISK" if case_index == 0 else "SLA_BREACHED"]
    elif invalid == "extra":
        payload["approved"] = True
    elif invalid == "evidence":
        payload["evidence"]["ticket_id"] = 42
    elif invalid == "missing":
        payload["evidence"].pop(next(iter(payload["evidence"])))
    else:
        key = next(iter(payload["evidence"]))
        payload["evidence"][key] = True if invalid == "boolean" else -1
    with pytest.raises(ValidationError):
        TypeAdapter(ActionRecommendation).validate_python(payload)


def test_recommendation_values_are_immutable():
    metrics = RiskMetrics(1, 1, 0, 0, 1, 0)
    item, = build_recommendations(metrics, RiskPolicy(), 360, evaluate_risk(metrics, RiskPolicy(), 360))
    with pytest.raises(ValidationError, match="frozen"):
        item.priority = "high"
    with pytest.raises(ValidationError, match="frozen"):
        item.evidence.unassigned_tickets = 99
