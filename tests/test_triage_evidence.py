"""Independently authored synthetic snapshots, with no engine/SQL in contract tests."""

import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from ai_operations_agent.agent_evidence import GroundingError
from ai_operations_agent.agent_schemas import AgentResponse, EvidenceSource, StructuredAnswerDraft
from ai_operations_agent.risk import RiskPolicy
from ai_operations_agent.tool_schemas import AccessContext, ExecutionLimits
from ai_operations_agent.tools import EvaluationContext
from ai_operations_agent.triage_agent_schemas import (
    MAX_REFERENCES,
    LinkedDetailSnapshot,
    TriageAgentResponse,
    TriageEvidenceItem,
    TriageSelection,
    TriageSnapshot,
    TriageSource,
)
from ai_operations_agent.triage_evidence import (
    TriageEvidenceLedger,
    build_triage_answer,
    build_triage_inability,
    validate_triage_response,
)

WHEN = "2026-10-31T16:00:00Z"
CONTEXT = EvaluationContext(datetime(2026, 10, 31, 16, tzinfo=timezone.utc), RiskPolicy())
ALL_SIGNALS = [
    dict(
        code="SLA_BREACHED",
        severity="high",
        affected_tickets=1,
        reason="1 active ticket is past its SLA deadline.",
    ),
    dict(
        code="SLA_AT_RISK",
        severity="medium",
        affected_tickets=1,
        reason="1 active ticket is due now or within 360 seconds.",
    ),
    dict(
        code="UNASSIGNED_BACKLOG",
        severity="low",
        affected_tickets=2,
        reason="2 active tickets have no assigned agent.",
    ),
    dict(
        code="NO_AGENTS_ONLINE",
        severity="medium",
        affected_tickets=2,
        reason="No agents are recorded online for this queue's 2 active tickets.",
    ),
]


def assessment(qid=20, *, mode="all"):
    """Literal independent facts and rule expectations, not production helper output."""
    return dict(
        queue_id=qid,
        queue_name=f"Synthetic {qid}",
        evaluated_at=WHEN,
        sla_minutes=30,
        policy=dict(
            version="queue-risk-v1",
            sla_at_risk_remaining_percent=20,
            sla_at_risk_window_seconds=360.0,
            unassigned_backlog_min_tickets=1,
        ),
        metrics=dict(
            active_tickets=2 if mode == "all" else 1 if mode == "high" else 0,
            unassigned_tickets=2 if mode == "all" else 0,
            sla_breached_tickets=0 if mode == "empty" else 1,
            sla_at_risk_tickets=1 if mode == "all" else 0,
            online_agents=0 if mode == "all" else 1,
            future_created_active_tickets=0,
        ),
        overall_severity="none" if mode == "empty" else "high",
        signals=deepcopy(
            ALL_SIGNALS if mode == "all" else ALL_SIGNALS[:1] if mode == "high" else []
        ),
    )


def triage_snapshot(*, mode="multi", count=3):
    if mode == "multi":
        rows = [assessment(), assessment(10, mode="high"), assessment(30, mode="empty")]
        scope, missing, eligible, target, outcome = (
            [10, 20, 30, 40],
            [40],
            2,
            20,
            "attention_required",
        )
    elif mode == "missing":
        rows, scope, missing, eligible, target, outcome = (
            [],
            [10, 40],
            [10, 40],
            0,
            None,
            "no_available_queues",
        )
    elif mode == "empty":
        rows = [assessment(10, mode="empty")]
        scope, missing, eligible, target, outcome = [10], [], 0, None, "no_triggered_rules"
    else:
        rows = [assessment(qid, mode="all") for qid in range(1, count + 1)]
        scope, missing, eligible, target, outcome = (
            list(range(1, count + 1)),
            [],
            count,
            1,
            "attention_required",
        )
    return dict(
        evaluated_at=WHEN,
        ranking_policy_version="queue-triage-v1",
        risk_policy=dict(
            version="queue-risk-v1",
            sla_at_risk_remaining_percent=20,
            unassigned_backlog_min_tickets=1,
        ),
        scope=dict(mode="authorized_scope", effective_queue_ids=scope, selected_queue_id=None),
        unavailable_queue_ids=missing,
        available_queue_count=len(rows),
        unavailable_queue_count=len(missing),
        rankings=[
            dict(
                position=i,
                assessment=a,
                operational_tie=mode == "tied" and count > 1,
                detail_eligible=bool(a["signals"]),
                data_warnings=[],
            )
            for i, a in enumerate(rows, 1)
        ],
        eligible_queue_count=eligible,
        detail_target_queue_id=target,
        outcome=outcome,
    )


def detail_snapshot(qid=20):
    return dict(
        recommendation_policy_version="queue-recommendations-v1",
        advisory_only=True,
        requires_human_review=True,
        assessment=assessment(qid),
        recommendations=[
            dict(
                code="REVIEW_OVERDUE_TICKETS",
                priority="high",
                triggered_by_signals=["SLA_BREACHED"],
                suggested_action="Review overdue active tickets and decide the next response.",
                explanation=ALL_SIGNALS[0]["reason"],
                evidence=dict(sla_breached_tickets=1),
            ),
            dict(
                code="REVIEW_UPCOMING_DEADLINES",
                priority="medium",
                triggered_by_signals=["SLA_AT_RISK"],
                suggested_action="Review active tickets due now or within the warning window.",
                explanation=ALL_SIGNALS[1]["reason"],
                evidence=dict(sla_at_risk_tickets=1, sla_at_risk_window_seconds=360.0),
            ),
            dict(
                code="VERIFY_RECORDED_AGENT_STATUS",
                priority="medium",
                triggered_by_signals=["NO_AGENTS_ONLINE"],
                suggested_action="Verify recorded agent status and confirm actual availability.",
                explanation=ALL_SIGNALS[3]["reason"]
                + " Recorded status does not establish actual availability.",
                evidence=dict(active_tickets=2, online_agents=0),
            ),
            dict(
                code="TRIAGE_UNASSIGNED_TICKETS",
                priority="low",
                triggered_by_signals=["UNASSIGNED_BACKLOG"],
                suggested_action="Review unassigned active tickets and decide whether assignment is appropriate.",
                explanation=ALL_SIGNALS[2]["reason"],
                evidence=dict(unassigned_tickets=2, unassigned_backlog_min_tickets=1),
            ),
        ],
    )


def ledger_for(snapshot=None, *, ids=None, selected=None, names=None):
    snapshot = triage_snapshot() if snapshot is None else snapshot
    ids = snapshot["scope"]["effective_queue_ids"] if ids is None else ids
    access = AccessContext(
        allowed_queue_ids=frozenset(ids),
        selected_queue_id=selected,
        allowed_tool_names=frozenset(names or {"get_queue_triage", "get_queue_recommendations"}),
        limits=ExecutionLimits(max_result_bytes=131072),
    )
    source_ids = iter(["triage-source", "detail-source", "extra-source"])
    ledger = TriageEvidenceLedger("run-v2", access, CONTEXT, lambda: next(source_ids))
    ledger.capture("get_queue_triage", snapshot)
    return ledger


def test_independent_fixture_rule_text():
    # Fail immediately if the literal fixture disagrees with the strict engine contract.
    from ai_operations_agent.triage_schemas import QueueTriageResult

    value = QueueTriageResult.model_validate_json(json.dumps(triage_snapshot()), strict=True)
    assert [r.assessment.queue_id for r in value.rankings] == [20, 10, 30]


def test_multi_queue_evidence_exact_values_pointers_and_order():
    ledger = ledger_for()
    response = build_triage_answer(ledger)
    assert response.schema_version == "offline-agent-v2" and response.status == "answered"
    assert [(i.reference.kind, i.reference.pointer, i.queue_id) for i in response.evidence] == [
        ("triage_summary", "/summary", None),
        ("triage_queue", "/queues/0", 20),
        ("triage_queue", "/queues/1", 10),
        ("triage_queue", "/queues/2", 30),
    ]
    expected = triage_snapshot()
    rows = expected.pop("rankings")
    assert response.evidence[0].value == expected
    assert [item.value for item in response.evidence[1:]] == rows
    assert response.evidence[1].value["assessment"]["metrics"]["sla_breached_tickets"] == 1
    assert [b.kind for b in response.explanation] == [
        "triage_summary",
        "ranked_queue",
        "ranked_queue",
        "ranked_queue",
    ]
    assert response.explanation[0].text == (
        "Authorized scope: 4 queues; available: 3; unavailable: 1; eligible for detail: 2. "
        "Evaluated at 2026-10-31T16:00:00+00:00; ranking policy queue-triage-v1."
    )
    assert (
        TriageAgentResponse.model_validate_json(response.model_dump_json(), strict=True) == response
    )
    ledger.validate(response)


@pytest.mark.parametrize(
    "mode,kind,count", [("missing", "no_available_queues", 1), ("empty", "no_rules", 2)]
)
def test_empty_outcomes_keep_complete_summary(mode, kind, count):
    response = build_triage_answer(ledger_for(triage_snapshot(mode=mode)))
    assert len(response.evidence) == count
    assert response.evidence[0].queue_id is None
    assert response.explanation[-1].kind == kind
    assert response.evidence[0].value["outcome"] == (
        "no_available_queues" if mode == "missing" else "no_triggered_rules"
    )


def test_max_scope_ties_and_linked_detail_has_38_references():
    ledger = ledger_for(triage_snapshot(mode="tied", count=32))
    ledger.capture(
        "get_queue_recommendations", detail_snapshot(1), parent_triage_source_id="triage-source"
    )
    response = build_triage_answer(ledger, TriageSelection(references=ledger.references()))
    assert MAX_REFERENCES == len(response.evidence) == 38
    assert [i.queue_id for i in response.evidence[1:33]] == list(range(1, 33))
    assert all(i.value["operational_tie"] for i in response.evidence[1:33])
    assert [i.reference.pointer for i in response.evidence[-5:]] == [
        "/assessment",
        "/recommendations/0",
        "/recommendations/1",
        "/recommendations/2",
        "/recommendations/3",
    ]
    with pytest.raises(ValidationError):
        StructuredAnswerDraft.model_validate_json(
            json.dumps(
                dict(
                    references=[dict(source_id="s", kind="assessment", pointer="/assessment")] * 33,
                    kind="queue_focus",
                )
            ),
            strict=True,
        )


def test_linked_detail_is_exact_target_and_assessment_with_all_recommendations():
    ledger = ledger_for()
    source = ledger.capture(
        "get_queue_recommendations", detail_snapshot(), parent_triage_source_id="triage-source"
    )
    response = build_triage_answer(ledger)
    assert source.snapshot.assessment == ledger.sources[0].snapshot.queues[0].assessment
    assert [(i.reference.kind, i.queue_id) for i in response.evidence[4:]] == [
        ("assessment", 20),
        *(("recommendation", 20) for _ in range(4)),
    ]
    assert [i.value["code"] for i in response.evidence[-4:]] == [
        "REVIEW_OVERDUE_TICKETS",
        "REVIEW_UPCOMING_DEADLINES",
        "VERIFY_RECORDED_AGENT_STATUS",
        "TRIAGE_UNASSIGNED_TICKETS",
    ]
    assert all(len(b.references) == 2 for b in response.explanation if b.kind == "recommendations")
    with pytest.raises(GroundingError, match="invalid linked source"):
        ledger.capture(
            "get_queue_recommendations", detail_snapshot(), parent_triage_source_id="triage-source"
        )
    assert len(ledger.sources) == 2


def test_capture_detaches_and_deeply_freezes_snapshots():
    original = triage_snapshot()
    ledger = ledger_for(original)
    original["rankings"][0]["assessment"]["queue_name"] = "Mutated input"
    source = ledger.sources[0]
    assert source.snapshot.queues[0].assessment.queue_name == "Synthetic 20"
    with pytest.raises(ValidationError):
        source.snapshot.queues[0].assessment.metrics.active_tickets = 999
    with pytest.raises(ValidationError):
        source.snapshot.summary.scope.effective_queue_ids = (999,)
    with pytest.raises(ValidationError):
        source.snapshot.queues[0].assessment.signals[0].reason = "forged"
    detail = detail_snapshot()
    linked = ledger.capture(
        "get_queue_recommendations", detail, parent_triage_source_id="triage-source"
    )
    detail["assessment"]["queue_name"] = "Mutated detail"
    with pytest.raises(ValidationError):
        linked.snapshot.recommendations[0].evidence.sla_breached_tickets = 999
    assert ledger.sources[1].snapshot.assessment.queue_name == "Synthetic 20"


@pytest.mark.parametrize(
    "change",
    [
        "scope",
        "missing",
        "count",
        "rank",
        "tie",
        "eligibility",
        "target",
        "outcome",
        "policy",
        "time",
        "signal",
        "metric",
        "extra",
        "bool",
        "queue_id",
        "version",
    ],
)
def test_invalid_triage_snapshot_is_rejected_atomically(change):
    value = triage_snapshot()
    if change == "scope":
        value["scope"]["effective_queue_ids"].append(50)
    elif change == "missing":
        value["unavailable_queue_ids"] = []
    elif change == "count":
        value["available_queue_count"] = 2
    elif change == "rank":
        value["rankings"].reverse()
    elif change == "tie":
        value["rankings"][0]["operational_tie"] = True
    elif change == "eligibility":
        value["rankings"][0]["detail_eligible"] = False
    elif change == "target":
        value["detail_target_queue_id"] = 10
    elif change == "outcome":
        value["outcome"] = "no_triggered_rules"
    elif change == "policy":
        value["risk_policy"]["unassigned_backlog_min_tickets"] = 3
    elif change == "time":
        value["rankings"][0]["assessment"]["evaluated_at"] = "2026-10-30T16:00:00Z"
    elif change == "signal":
        value["rankings"][0]["assessment"]["signals"].pop()
    elif change == "metric":
        value["rankings"][0]["assessment"]["metrics"]["active_tickets"] = -1
    elif change == "extra":
        value["rankings"][0]["assessment"]["metrics"]["secret"] = "PRIVATE"
    elif change == "bool":
        value["rankings"][0]["position"] = True
    elif change == "queue_id":
        value["rankings"][0]["assessment"]["queue_id"] = 0
    elif change == "version":
        value["ranking_policy_version"] = "queue-triage-v99"
    with pytest.raises(GroundingError):
        ledger_for(value)


@pytest.mark.parametrize("change", ["allowed", "selected", "time", "policy"])
def test_self_consistent_snapshot_still_requires_exact_trusted_context(change):
    value = triage_snapshot()
    ids = None
    if change == "allowed":
        ids = {10, 20, 30, 40, 50}
    elif change == "selected":
        with pytest.raises(GroundingError):
            ledger_for(value, selected=20)
        return
    elif change == "time":
        value["evaluated_at"] = "2026-10-30T16:00:00Z"
        for row in value["rankings"]:
            row["assessment"]["evaluated_at"] = value["evaluated_at"]
    else:
        value = triage_snapshot(mode="empty")
        value["risk_policy"]["unassigned_backlog_min_tickets"] = 2
        value["rankings"][0]["assessment"]["policy"]["unassigned_backlog_min_tickets"] = 2
    with pytest.raises(GroundingError, match="source outside trusted context"):
        ledger_for(value, ids=ids)


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "reorder",
        "pointer",
        "source",
        "run",
        "queue",
        "summary_queue",
        "missing_queue",
        "value",
        "block_ref",
        "block_text",
        "blocks",
        "safety",
        "version",
        "source_id",
        "uncertainty",
    ],
)
def test_independent_response_validator_rejects_each_tampering(change):
    response = build_triage_answer(ledger_for())
    value = response.model_dump(mode="json")
    if change == "missing":
        value["evidence"].pop()
    elif change == "duplicate":
        value["evidence"].append(deepcopy(value["evidence"][0]))
    elif change == "reorder":
        value["evidence"][1:3] = reversed(value["evidence"][1:3])
    elif change == "pointer":
        value["evidence"][1]["reference"]["pointer"] = "/rankings/0"
    elif change == "source":
        value["evidence"][1]["reference"]["source_id"] = "forged"
    elif change == "run":
        value["sources"][0]["run_id"] = "other-run"
    elif change == "queue":
        value["evidence"][1]["queue_id"] = 999
    elif change == "summary_queue":
        value["evidence"][0]["queue_id"] = 20
    elif change == "missing_queue":
        value["evidence"][1]["queue_id"] = None
    elif change == "value":
        value["evidence"][1]["value"]["position"] = 5
    elif change == "block_ref":
        value["explanation"][1]["references"][0]["pointer"] = "/queues/1"
    elif change == "block_text":
        value["explanation"][1]["text"] = "Model authored ranking"
    elif change == "blocks":
        value["explanation"].reverse()
    elif change == "safety":
        value["read_only"] = 1
    elif change == "version":
        value["schema_version"] = "offline-agent-v1"
    elif change == "source_id":
        value["sources"][0]["source_id"] = "run-v2"
    else:
        value["uncertainty"] = []
    with pytest.raises(ValidationError):
        TriageAgentResponse.model_validate_json(json.dumps(value), strict=True)


def test_independent_validator_does_not_trust_model_copy_or_construct():
    response = build_triage_answer(ledger_for())
    forged = response.model_copy(update={"evidence": response.evidence[:-1]})
    with pytest.raises(GroundingError, match="invalid triage response"):
        validate_triage_response(forged)
    with pytest.raises(GroundingError):
        build_triage_answer(
            ledger_for(), TriageSelection(references=tuple(reversed(ledger_for().references())))
        )


def test_self_consistent_forgery_requires_private_ledger_equality():
    ledger = ledger_for()
    other = triage_snapshot()
    other["rankings"][0]["assessment"]["queue_name"] = "Forged but self-consistent"
    forged_response = build_triage_answer(ledger_for(other))
    validate_triage_response(forged_response)  # Structure cannot prove capture provenance.
    with pytest.raises(GroundingError, match="private ledger"):
        ledger.validate(forged_response)


@pytest.mark.parametrize(
    "change",
    [
        "target",
        "parent",
        "run",
        "time",
        "metric",
        "signal",
        "recommendation_missing",
        "recommendation_order",
        "recommendation_text",
        "extra",
        "strict_float",
        "flags",
    ],
)
def test_invalid_linked_detail_capture_and_response_checks(change):
    ledger = ledger_for()
    detail = detail_snapshot()
    parent = "triage-source"
    if change == "target":
        detail = detail_snapshot(10)  # Still authorized, still rejected.
    elif change == "parent":
        parent = "stale-source"
    elif change == "run":
        ledger.capture("get_queue_recommendations", detail, parent_triage_source_id=parent)
        value = build_triage_answer(ledger).model_dump(mode="json")
        value["sources"][1]["run_id"] = "other-run"
        with pytest.raises(ValidationError):
            TriageAgentResponse.model_validate_json(json.dumps(value), strict=True)
        return
    elif change == "time":
        detail["assessment"]["evaluated_at"] = "2026-10-30T16:00:00Z"
    elif change == "metric":
        detail["assessment"]["metrics"]["online_agents"] = 1
    elif change == "signal":
        detail["assessment"]["signals"][0]["reason"] = "forged"
    elif change == "recommendation_missing":
        detail["recommendations"].pop()
    elif change == "recommendation_order":
        detail["recommendations"].reverse()
    elif change == "recommendation_text":
        detail["recommendations"][0]["suggested_action"] = "Make autonomous writes"
    elif change == "extra":
        detail["assessment"]["credentials"] = "PRIVATE"
    elif change == "strict_float":
        detail["recommendations"][1]["evidence"]["sla_at_risk_window_seconds"] = "360"
    else:
        detail["advisory_only"] = 1
    with pytest.raises(GroundingError):
        ledger.capture("get_queue_recommendations", detail, parent_triage_source_id=parent)
    assert len(ledger.sources) == 1


@pytest.mark.parametrize(
    "change", ["metric", "assessment", "parent", "missing", "reorder", "duplicate"]
)
def test_independent_linked_response_validator(change):
    ledger = ledger_for()
    ledger.capture(
        "get_queue_recommendations", detail_snapshot(), parent_triage_source_id="triage-source"
    )
    value = build_triage_answer(ledger).model_dump(mode="json")
    if change == "metric":
        value["sources"][1]["snapshot"]["assessment"]["metrics"]["online_agents"] = 1
    elif change == "assessment":
        value["sources"][1]["snapshot"]["assessment"]["queue_name"] = "Different"
    elif change == "parent":
        value["sources"][1]["parent_triage_source_id"] = "stale-source"
    elif change == "missing":
        value["sources"][1]["snapshot"]["recommendations"].pop()
    elif change == "reorder":
        value["sources"][1]["snapshot"]["recommendations"].reverse()
    else:
        value["sources"].append(deepcopy(value["sources"][1]))
    with pytest.raises(ValidationError):
        TriageAgentResponse.model_validate_json(json.dumps(value), strict=True)


def test_changed_valid_detail_metric_is_not_the_same_assessment():
    ledger = ledger_for()
    detail = detail_snapshot()
    detail["assessment"]["metrics"]["future_created_active_tickets"] = 1
    detail["assessment"]["metrics"]["active_tickets"] = 3
    detail["assessment"]["signals"][-1]["affected_tickets"] = 3
    detail["assessment"]["signals"][-1]["reason"] = (
        "No agents are recorded online for this queue's 3 active tickets."
    )
    detail["recommendations"][2]["evidence"]["active_tickets"] = 3
    detail["recommendations"][2]["explanation"] = (
        detail["assessment"]["signals"][-1]["reason"]
        + " Recorded status does not establish actual availability."
    )
    # A different internally canonical assessment is invalid even though its rules match.
    LinkedDetailSnapshot.model_validate_json(json.dumps(detail), strict=True)
    with pytest.raises(GroundingError, match="different detail assessment"):
        ledger.capture("get_queue_recommendations", detail, parent_triage_source_id="triage-source")


@pytest.mark.parametrize(
    "status", ["error", "unsupported", "insufficient_information", "needs_clarification"]
)
def test_inability_has_fixed_text_and_no_partial_operational_claims(status):
    response = build_triage_inability("run-v2", status)
    assert not response.sources and not response.evidence
    value = response.model_dump(mode="json")
    value["sources"] = build_triage_answer(ledger_for()).model_dump(mode="json")["sources"]
    with pytest.raises(ValidationError):
        TriageAgentResponse.model_validate_json(json.dumps(value), strict=True)
    value = response.model_dump(mode="json")
    value["explanation"][0]["text"] = "PRIVATE raw error"
    with pytest.raises(ValidationError):
        TriageAgentResponse.model_validate_json(json.dumps(value), strict=True)


def test_sanitized_errors_and_no_arbitrary_error_text():
    value = build_triage_inability("run-v2").model_dump(mode="json")
    value["error"]["message"] = "PRIVATE database configuration"
    with pytest.raises(ValidationError):
        TriageAgentResponse.model_validate_json(json.dumps(value), strict=True)


def test_sources_and_response_sizes_are_bounded_and_atomic():
    ledger = ledger_for()
    before = ledger.sources
    value = detail_snapshot()
    value["recommendations"][0]["suggested_action"] = "x" * 131073
    with pytest.raises(GroundingError, match="size limit"):
        ledger.capture("get_queue_recommendations", value, parent_triage_source_id="triage-source")
    assert ledger.sources == before
    # Independent validation bounds the complete response, even a forged constructed object.
    response = build_triage_answer(ledger)
    forged = response.model_copy(update={"uncertainty": ("x" * 524289,)})
    with pytest.raises(GroundingError, match="size limit"):
        validate_triage_response(forged)
    source = ledger.sources[0].model_dump(mode="json")
    source["snapshot"]["summary"]["extra"] = "x" * 131073
    with pytest.raises(ValidationError):
        TriageSource.model_validate_json(json.dumps(source), strict=True)


def test_v1_v2_sources_and_responses_cannot_use_wrong_validator():
    response = build_triage_answer(ledger_for())
    with pytest.raises(ValidationError):
        AgentResponse.model_validate_json(response.model_dump_json(), strict=True)
    with pytest.raises(ValidationError):
        EvidenceSource.model_validate_json(response.sources[0].model_dump_json(), strict=True)
    legacy = EvidenceSource(
        source_id="s",
        run_id="run-v2",
        tool_name="get_queue_recommendations",
        snapshot=detail_snapshot(),
    )
    value = response.model_dump(mode="json")
    value["sources"].append(legacy.model_dump(mode="json"))
    with pytest.raises(ValidationError):
        TriageAgentResponse.model_validate_json(json.dumps(value), strict=True)
    schema = AgentResponse.model_json_schema()
    assert schema["$defs"]["EvidenceSource"]["properties"]["tool_name"]["enum"] == [
        "list_queues",
        "get_queue_recommendations",
    ]
    assert StructuredAnswerDraft.model_fields["references"].metadata[0].max_length == 32


@pytest.mark.parametrize("name", ["list_queues", "arbitrary_sql", "unsupported"])
def test_unsupported_source_types(name):
    with pytest.raises(GroundingError, match="unsupported source"):
        ledger_for().capture(name, {})


def test_duplicate_source_ids_and_detail_grants():
    access = AccessContext(
        allowed_queue_ids=frozenset({10, 20, 30, 40}),
        allowed_tool_names=frozenset({"get_queue_triage", "get_queue_recommendations"}),
    )
    ledger = TriageEvidenceLedger("run-v2", access, CONTEXT, lambda: "same-id")
    ledger.capture("get_queue_triage", triage_snapshot())
    with pytest.raises(GroundingError, match="provenance"):
        ledger.capture(
            "get_queue_recommendations", detail_snapshot(), parent_triage_source_id="same-id"
        )
    restricted = ledger_for(names={"get_queue_triage"})
    with pytest.raises(GroundingError):
        restricted.capture(
            "get_queue_recommendations", detail_snapshot(), parent_triage_source_id="triage-source"
        )


def test_v2_level_types_forbid_missing_or_nonpositive_queue_id():
    value = build_triage_answer(ledger_for()).evidence[1].model_dump(mode="json")
    for qid in [None, 0, -1, True, "20"]:
        with pytest.raises(ValidationError):
            TriageEvidenceItem.model_validate_json(
                json.dumps(dict(value, queue_id=qid)), strict=True
            )
    del value["queue_id"]
    with pytest.raises(ValidationError):
        TriageEvidenceItem.model_validate_json(json.dumps(value), strict=True)


def test_direct_snapshot_validation_checks_rankings_independently():
    value = build_triage_answer(ledger_for()).sources[0].snapshot.model_dump(mode="json")
    value["queues"][0]["assessment"]["queue_id"] = 999
    with pytest.raises(ValidationError):
        TriageSnapshot.model_validate_json(json.dumps(value), strict=True)


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_json_evidence_values_have_strict_canonical_types(value):
    response = build_triage_answer(ledger_for()).model_dump(mode="json")
    response["evidence"][1]["value"]["assessment"]["metrics"]["sla_breached_tickets"] = value
    with pytest.raises(ValidationError):
        TriageAgentResponse.model_validate_json(json.dumps(response), strict=True)


def test_canonical_source_size_limit_is_independently_enforced():
    from ai_operations_agent.triage_schemas import QueueTriageResult

    value = triage_snapshot(mode="tied", count=32)
    large = 10**400
    for row in value["rankings"]:
        a = row["assessment"]
        a["metrics"].update(
            active_tickets=2 * large,
            unassigned_tickets=2 * large,
            sla_breached_tickets=large,
            sla_at_risk_tickets=large,
        )
        amounts = [large, large, 2 * large, 2 * large]
        reasons = [
            f"{large} active tickets are past their SLA deadline.",
            f"{large} active tickets are due now or within 360 seconds.",
            f"{2 * large} active tickets have no assigned agent.",
            f"No agents are recorded online for this queue's {2 * large} active tickets.",
        ]
        for signal, amount, reason in zip(a["signals"], amounts, reasons, strict=True):
            signal.update(affected_tickets=amount, reason=reason)
    QueueTriageResult.model_validate_json(json.dumps(value), strict=True)
    queues = value.pop("rankings")
    source = dict(
        schema_version="triage-evidence-v2",
        source_id="s",
        run_id="r",
        tool_name="get_queue_triage",
        snapshot=dict(summary=value, queues=queues),
    )
    with pytest.raises(ValidationError, match="source exceeds size limit"):
        TriageSource.model_validate_json(json.dumps(source), strict=True)


@pytest.mark.parametrize("mode", ["missing", "empty"])
def test_linked_detail_without_eligible_target_is_rejected(mode):
    ledger = ledger_for(triage_snapshot(mode=mode))
    with pytest.raises(GroundingError, match="invalid detail target"):
        ledger.capture(
            "get_queue_recommendations", detail_snapshot(), parent_triage_source_id="triage-source"
        )
    assert len(ledger.sources) == 1


def test_source_order_and_duplicate_triage_are_rejected():
    ledger = ledger_for()
    with pytest.raises(GroundingError, match="duplicate triage source"):
        ledger.capture("get_queue_triage", triage_snapshot())
    ledger.capture(
        "get_queue_recommendations", detail_snapshot(), parent_triage_source_id="triage-source"
    )
    response = build_triage_answer(ledger).model_dump(mode="json")
    response["sources"].reverse()
    with pytest.raises(ValidationError):
        TriageAgentResponse.model_validate_json(json.dumps(response), strict=True)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "offline-agent-v1"),
        ("read_only", False),
        ("advisory_only", 1),
        ("requires_human_review", False),
        ("extra", "PRIVATE"),
    ],
)
def test_independent_validator_revalidates_all_response_fields(field, value):
    ledger = ledger_for()
    response = build_triage_answer(ledger).model_copy(update={field: value})
    with pytest.raises(GroundingError):
        validate_triage_response(response)
    with pytest.raises(GroundingError):
        ledger.validate(response)


def test_independent_validator_rejects_hidden_extra_fields_on_copied_source():
    response = build_triage_answer(ledger_for())
    source = response.sources[0].model_copy(update={"extra": "PRIVATE"})
    with pytest.raises(GroundingError):
        validate_triage_response(response.model_copy(update={"sources": (source,)}))


@pytest.mark.parametrize("nested", [False, True])
def test_copied_recommendations_cannot_silently_drop_hidden_invalid_fields(nested):
    ledger = ledger_for()
    ledger.capture(
        "get_queue_recommendations", detail_snapshot(), parent_triage_source_id="triage-source"
    )
    response = build_triage_answer(ledger)
    source = response.sources[1]
    recommendation = source.snapshot.recommendations[0]
    if nested:
        recommendation = recommendation.model_copy(
            update={
                "evidence": recommendation.evidence.model_copy(update={"extra": "PRIVATE"}),
            }
        )
    else:
        recommendation = recommendation.model_copy(update={"extra": "PRIVATE"})
    snapshot = source.snapshot.model_copy(
        update={
            "recommendations": (recommendation, *source.snapshot.recommendations[1:]),
        }
    )
    source = source.model_copy(update={"snapshot": snapshot})
    with pytest.raises(GroundingError):
        validate_triage_response(
            response.model_copy(update={"sources": (response.sources[0], source)})
        )
