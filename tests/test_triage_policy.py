"""Independent examples for the proposed policy and strict internal contracts."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import permutations

import pytest
from pydantic import ValidationError

from ai_operations_agent.metrics import QueueRiskFacts
from ai_operations_agent.queue_triage import build_triage_result
from ai_operations_agent.risk import RiskMetrics, RiskPolicy
from ai_operations_agent.triage_schemas import QueueTriageResult, TriageScope

NOW = datetime(2026, 10, 31, 16, 0, 0, 123456, tzinfo=timezone.utc)


def fact(qid, *, active=4, breached=0, upcoming=0, unassigned=0, online=1, future=0,
         policy=RiskPolicy()):
    return QueueRiskFacts(qid, f"Queue {qid}", 30, policy.warning_window(30).total_seconds(),
                          RiskMetrics(active, unassigned, breached, upcoming, online, future))


def build(*facts, policy=RiskPolicy(), ids=None):
    scope = TriageScope(mode="authorized_scope", effective_queue_ids=tuple(sorted(
        ids if ids is not None else {f.queue_id for f in facts}
    )))
    return build_triage_result(tuple(facts), scope, NOW, policy)


@pytest.mark.parametrize(("left", "right"), [
    # The winner has the larger ID, so the operational factor must decide.
    (dict(active=1, breached=1), dict(active=20, upcoming=20)),  # severity
    (dict(breached=2), dict(breached=1, upcoming=2)),  # breached
    (dict(breached=1, upcoming=2), dict(breached=1, upcoming=1)),  # at risk
    (dict(upcoming=1, online=0), dict(upcoming=1, unassigned=4)),  # no online
    (dict(breached=1, unassigned=3), dict(breached=1, unassigned=2)),  # unassigned
    (dict(active=5), dict(active=4)),  # active
])
def test_each_lexicographic_dimension(left, right):
    result = build(fact(9, **left), fact(1, **right))
    assert [r.assessment.queue_id for r in result.rankings] == [9, 1]
    assert not any(r.operational_tie for r in result.rankings)


def test_input_order_invariance_and_multiple_high_risk_queues():
    facts = (fact(4, breached=2), fact(2, breached=1, upcoming=2),
             fact(3, breached=1, upcoming=1), fact(1, upcoming=4))
    baseline = None
    for order in permutations(facts):
        result = build(*order)
        assert [r.assessment.queue_id for r in result.rankings] == [4, 2, 3, 1]
        assert [r.position for r in result.rankings] == [1, 2, 3, 4]
        assert result.detail_target_queue_id == 4 and result.eligible_queue_count == 4
        serialized = result.model_dump_json()
        baseline = serialized if baseline is None else baseline
        assert serialized == baseline


def test_operational_ties_do_not_include_id_and_below_threshold_counts():
    policy = RiskPolicy(unassigned_min_tickets=3)
    result = build(fact(9, unassigned=2, policy=policy), fact(1, policy=policy), policy=policy)
    assert [r.assessment.queue_id for r in result.rankings] == [1, 9]
    assert all(r.operational_tie for r in result.rankings)
    assert not any(r.detail_eligible for r in result.rankings)
    assert result.detail_target_queue_id is None and result.outcome == "no_triggered_rules"


def test_medium_sla_priority_precedes_no_online_volume():
    result = build(fact(9, active=1, upcoming=1), fact(1, active=100, online=0))
    assert [r.assessment.queue_id for r in result.rankings] == [9, 1]


def test_no_online_affected_volume_precedes_assignment_backlog():
    result = build(fact(9, active=5, online=0), fact(1, active=4, online=0, unassigned=4))
    assert [r.assessment.queue_id for r in result.rankings] == [9, 1]


def test_overlapping_signals_are_separate_and_all_evidence_is_preserved():
    result = build(fact(1, active=2, breached=1, upcoming=1, unassigned=2, online=0))
    row = result.rankings[0]
    assert [s.affected_tickets for s in row.assessment.signals] == [1, 1, 2, 2]
    assert row.assessment.metrics.active_tickets == 2
    assert row.detail_eligible and result.outcome == "attention_required"
    assert set(row.model_dump()) == {
        "position", "assessment", "operational_tie", "detail_eligible", "data_warnings",
    }


def test_empty_no_rule_future_and_unavailable_classifications():
    empty = build(fact(1, active=0, online=0))
    assert empty.outcome == "no_triggered_rules" and empty.eligible_queue_count == 0
    missing = build(ids=(1, 2))
    assert missing.outcome == "no_available_queues"
    assert missing.unavailable_queue_ids == (1, 2)
    assert missing.available_queue_count == 0 and missing.unavailable_queue_count == 2
    future = build(fact(1, active=1, future=1, unassigned=1, online=0))
    row = future.rankings[0]
    assert row.data_warnings == ("future_created_active_tickets",)
    assert [s.code for s in row.assessment.signals] == ["UNASSIGNED_BACKLOG", "NO_AGENTS_ONLINE"]


@pytest.mark.parametrize("changes", [
    {"active_tickets": -1}, {"unassigned_tickets": 5}, {"sla_breached_tickets": 5},
    {"sla_breached_tickets": 3, "sla_at_risk_tickets": 2},
    {"sla_at_risk_tickets": 3, "future_created_active_tickets": 2},
    {"online_agents": -1}, {"active_tickets": True}, {"active_tickets": "4"},
    {"active_tickets": 4.0}, {"active_tickets": None},
])
def test_invalid_aggregate_facts_are_rejected_without_coercion(changes):
    original = fact(1)
    bad = replace(original, metrics=replace(original.metrics, **changes))
    with pytest.raises(ValidationError):
        build(bad)


@pytest.mark.parametrize("changes", [
    {"queue_id": True}, {"queue_id": "1"}, {"sla_minutes": True},
    {"sla_minutes": 0}, {"queue_name": ""}, {"queue_name": "x" * 81},
    {"warning_window_seconds": float("nan")}, {"warning_window_seconds": float("inf")},
    {"warning_window_seconds": 1.0},
])
def test_invalid_queue_facts_are_rejected(changes):
    with pytest.raises((ValidationError, ValueError)):
        build(replace(fact(1), **changes), ids=(1,))


def corrupt(raw, mutation):
    a = raw["rankings"][0]["assessment"]
    if mutation == "duplicate_queue":
        raw["rankings"].append(raw["rankings"][0])
    elif mutation == "order":
        raw["rankings"].reverse()
    elif mutation == "position":
        raw["rankings"][0]["position"] = 2
    elif mutation == "tie":
        raw["rankings"][0]["operational_tie"] = True
    elif mutation == "eligibility":
        raw["rankings"][0]["detail_eligible"] = False
    elif mutation == "warning":
        raw["rankings"][0]["data_warnings"] = ["future_created_active_tickets"]
    elif mutation == "target":
        raw["detail_target_queue_id"] = 2
    elif mutation == "count":
        raw["available_queue_count"] = 1
    elif mutation == "eligible_count":
        raw["eligible_queue_count"] = 1
    elif mutation == "missing_partition":
        raw["unavailable_queue_ids"] = [2]
    elif mutation == "out_of_scope":
        raw["scope"]["effective_queue_ids"] = [1]
    elif mutation == "scope_order":
        raw["scope"]["effective_queue_ids"] = [2, 1]
    elif mutation == "selected_scope":
        raw["scope"]["mode"] = "selected_queue"
        raw["scope"]["selected_queue_id"] = 1
    elif mutation == "outcome":
        raw["outcome"] = "no_triggered_rules"
    elif mutation == "version":
        raw["ranking_policy_version"] = "model-ranking"
    elif mutation == "time":
        a["evaluated_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif mutation == "offset":
        a["evaluated_at"] = NOW.astimezone(timezone(timedelta(hours=1))).isoformat()
    elif mutation == "policy":
        raw["risk_policy"]["unassigned_backlog_min_tickets"] = 2
    elif mutation == "signal_missing":
        a["signals"] = []
    elif mutation == "signal_duplicate":
        a["signals"].append(a["signals"][0])
    elif mutation == "signal_reason":
        a["signals"][0]["reason"] = "Invented operational claim"
    elif mutation == "signal_count":
        a["signals"][0]["affected_tickets"] = 99
    elif mutation == "signal_severity":
        a["signals"][0]["severity"] = "low"
    elif mutation == "severity":
        a["overall_severity"] = "none"
    elif mutation == "missing_metric":
        del a["metrics"]["active_tickets"]
    elif mutation == "extra_metric":
        a["metrics"]["score"] = 5


@pytest.mark.parametrize("mutation", [
    "duplicate_queue", "order", "position", "tie", "eligibility", "warning", "target", "count",
    "eligible_count", "missing_partition", "out_of_scope", "scope_order", "selected_scope",
    "outcome", "version", "time", "offset", "policy", "signal_missing", "signal_duplicate",
    "signal_reason", "signal_count", "signal_severity", "severity", "missing_metric", "extra_metric",
])
def test_result_revalidates_canonical_semantics(mutation):
    result = build(fact(1, breached=2), fact(2, breached=1))
    raw = result.model_dump(mode="json")
    corrupt(raw, mutation)
    with pytest.raises(ValidationError):
        QueueTriageResult.model_validate_json(json.dumps(raw), strict=True)


def test_contract_round_trip_and_deep_immutability():
    result = build(fact(1, breached=1), ids=(1, 2))
    assert QueueTriageResult.model_validate_json(result.model_dump_json(), strict=True) == result
    for value, field in (
        (result, "outcome"), (result.scope, "mode"), (result.risk_policy, "version"),
        (result.rankings[0], "position"), (result.rankings[0].assessment, "queue_id"),
        (result.rankings[0].assessment.metrics, "active_tickets"),
        (result.rankings[0].assessment.policy, "version"),
        (result.rankings[0].assessment.signals[0], "reason"),
    ):
        with pytest.raises(ValidationError):
            setattr(value, field, None)
    assert isinstance(result.rankings, tuple)
    assert isinstance(result.rankings[0].assessment.signals, tuple)


def test_duplicate_facts_cannot_be_hidden_by_sorting():
    with pytest.raises(ValidationError):
        build(fact(1), fact(1))
