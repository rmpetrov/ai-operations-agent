"""Real batch reads with owned synthetic rows on SQLite and isolated PostgreSQL."""

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ai_operations_agent import queue_triage, read_only
from ai_operations_agent.evaluation_fixtures import business_snapshot
from ai_operations_agent.models import Agent, Queue, Ticket
from ai_operations_agent.queue_triage import TriageError, triage_queues
from ai_operations_agent.risk import RiskPolicy
from ai_operations_agent.tool_schemas import AccessContext

NOW = datetime(2026, 10, 31, 16, 0, 0, 123456, tzinfo=timezone.utc)
MISSING_ID = 2_000_000_000


@dataclass
class OwnedQueues:
    engine: object
    prefix: str = field(default_factory=lambda: f"triage-{uuid4().hex[:12]}")
    ids: list[int] = field(default_factory=list)

    def queue(self, *, sla=30, online=1, tickets=()):
        with Session(self.engine) as seed:
            queue = Queue(name=f"{self.prefix}-{len(self.ids)}", sla_minutes=sla)
            seed.add(queue)
            seed.flush()
            self.ids.append(queue.id)
            agents = [Agent(name="Synthetic operator", queue_id=queue.id,
                            status="online" if i < online else "offline")
                      for i in range(max(1, online))]
            seed.add_all(agents)
            seed.flush()
            for i, ticket in enumerate(tickets):
                status = ticket.get("status", "open")
                seed.add(Ticket(
                    external_id=f"{self.prefix}-{len(self.ids)}-{i}", subject="Synthetic ticket",
                    queue_id=queue.id, assigned_agent_id=agents[0].id
                    if ticket.get("assigned", True) else None,
                    priority="normal", status=status, created_at=ticket["created_at"],
                    resolved_at=NOW if status == "resolved" else None,
                ))
            seed.commit()
            return queue.id

    def run(self, ids=None, *, selected=None, policy=RiskPolicy(), evaluated_at=NOW):
        return triage_queues(
            self.engine,
            AccessContext(allowed_queue_ids=frozenset(self.ids if ids is None else ids),
                          selected_queue_id=selected),
            evaluated_at=evaluated_at, policy=policy,
        )


@pytest.fixture
def triage_data(engine):
    before = business_snapshot(engine)
    data = OwnedQueues(engine)
    try:
        yield data
    finally:
        if data.ids:
            with engine.begin() as cleanup:
                cleanup.execute(delete(Ticket).where(Ticket.queue_id.in_(data.ids)))
                cleanup.execute(delete(Agent).where(Agent.queue_id.in_(data.ids)))
                cleanup.execute(delete(Queue).where(Queue.id.in_(data.ids)))
        assert business_snapshot(engine) == before


@contextmanager
def statements(engine):
    observed = []

    def record(conn, cursor, statement, params, context, executemany):
        observed.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield observed
    finally:
        event.remove(engine, "before_cursor_execute", record)


def business_selects(observed):
    return [sql for sql in observed if sql.lstrip().upper().startswith("SELECT ")]


def test_mixed_sla_exact_boundaries_custom_policy_and_independent_counts(triage_data):
    data = triage_data
    first = data.queue(sla=1, online=2, tickets=(
        dict(created_at=NOW - timedelta(seconds=60, microseconds=1), assigned=False),
        dict(created_at=NOW - timedelta(seconds=60)),
        dict(created_at=NOW - timedelta(seconds=40, microseconds=200000)),
        dict(created_at=NOW - timedelta(seconds=40, microseconds=199999)),
        dict(created_at=NOW),
        dict(created_at=NOW + timedelta(microseconds=1), assigned=False),
        dict(created_at=NOW - timedelta(days=1), status="resolved", assigned=False),
    ))
    second = data.queue(sla=3, tickets=(
        dict(created_at=NOW - timedelta(seconds=120)),
        dict(created_at=NOW - timedelta(seconds=120, microseconds=600000)),
    ))
    policy = RiskPolicy(sla_remaining_percent=33, unassigned_min_tickets=3)
    before = business_snapshot(data.engine)
    result = data.run(policy=policy)
    assert [r.assessment.queue_id for r in result.rankings] == [first, second]
    a, b = (r.assessment for r in result.rankings)
    assert a.metrics.model_dump() == dict(
        active_tickets=6, unassigned_tickets=2, sla_breached_tickets=1, sla_at_risk_tickets=2,
        online_agents=2, future_created_active_tickets=1,
    )
    assert b.metrics.model_dump() == dict(
        active_tickets=2, unassigned_tickets=0, sla_breached_tickets=0, sla_at_risk_tickets=1,
        online_agents=1, future_created_active_tickets=0,
    )
    assert a.policy.sla_at_risk_window_seconds == 19.8
    assert b.policy.sla_at_risk_window_seconds == 59.4
    assert [s.code for s in a.signals] == ["SLA_BREACHED", "SLA_AT_RISK"]
    assert result.rankings[0].data_warnings == ("future_created_active_tickets",)
    assert result.evaluated_at == a.evaluated_at == b.evaluated_at == NOW
    assert result.risk_policy.risk_policy() == policy
    assert business_snapshot(data.engine) == before


def test_empty_resolved_only_and_active_without_configured_signals(triage_data):
    data = triage_data
    empty = data.queue(online=0)
    resolved = data.queue(online=2, tickets=(
        dict(created_at=NOW - timedelta(days=1), status="resolved", assigned=False),
    ))
    active = data.queue(tickets=(dict(created_at=NOW - timedelta(minutes=5)),))
    result = data.run()
    assert [r.assessment.queue_id for r in result.rankings] == [active, empty, resolved]
    assert result.outcome == "no_triggered_rules"
    assert result.detail_target_queue_id is None and result.eligible_queue_count == 0
    assert [r.operational_tie for r in result.rankings] == [False, True, True]
    assert result.rankings[2].assessment.metrics.online_agents == 2
    assert not any(r.detail_eligible or r.assessment.signals for r in result.rankings)


def test_overlap_offline_assignment_and_future_semantics(triage_data):
    data = triage_data
    all_signals = data.queue(online=0, tickets=(
        dict(created_at=NOW - timedelta(minutes=31), assigned=False),
        dict(created_at=NOW - timedelta(minutes=25), assigned=False),
    ))
    future = data.queue(online=0, tickets=(dict(created_at=NOW + timedelta(days=1)),))
    result = data.run()
    assert result.detail_target_queue_id == all_signals
    assert [r.assessment.queue_id for r in result.rankings] == [all_signals, future]
    a, b = (r.assessment for r in result.rankings)
    assert [s.affected_tickets for s in a.signals] == [1, 1, 2, 2]
    assert a.metrics.active_tickets == 2
    # A nonnull offline assignment remains assigned, and future tickets cannot enter SLA buckets.
    assert b.metrics.unassigned_tickets == b.metrics.sla_breached_tickets == b.metrics.sla_at_risk_tickets == 0
    assert b.metrics.future_created_active_tickets == 1
    assert [s.code for s in b.signals] == ["NO_AGENTS_ONLINE"]


def test_hidden_queue_cannot_influence_ranking_or_counts_and_selected_scope(triage_data):
    data = triage_data
    visible = data.queue()
    other = data.queue(tickets=(dict(created_at=NOW - timedelta(minutes=25)),))
    hidden = data.queue(online=0, tickets=(
        dict(created_at=NOW - timedelta(days=1), assigned=False),
    ))
    with statements(data.engine) as observed:
        result = data.run((visible, other))
    assert [r.assessment.queue_id for r in result.rankings] == [other, visible]
    assert result.available_queue_count == 2 and result.eligible_queue_count == 1
    assert result.detail_target_queue_id == other
    assert hidden not in result.scope.effective_queue_ids
    assert "WHERE queues.id IN" in business_selects(observed)[0]
    assert "tickets.queue_id IN" in business_selects(observed)[1]
    assert "agents.queue_id IN" in business_selects(observed)[1]
    selected = data.run((visible, other), selected=visible)
    assert selected.scope.mode == "selected_queue"
    assert selected.scope.effective_queue_ids == (visible,)
    assert [r.assessment.queue_id for r in selected.rankings] == [visible]
    assert selected.outcome == "no_triggered_rules"


def test_missing_ids_are_preserved_and_no_available_is_valid(triage_data):
    data = triage_data
    qid = data.queue()
    result = data.run((qid, MISSING_ID))
    assert result.unavailable_queue_ids == (MISSING_ID,)
    assert result.available_queue_count == result.unavailable_queue_count == 1
    with statements(data.engine) as observed:
        empty = data.run((MISSING_ID,))
    assert len(business_selects(observed)) == 1
    assert empty.outcome == "no_available_queues" and empty.rankings == ()
    assert empty.unavailable_queue_ids == (MISSING_ID,)


@pytest.mark.parametrize("case", ["empty", "forbidden", "limit"])
def test_scope_preflight_performs_zero_connection_or_business_access(monkeypatch, case):
    class NoConnection:
        def connect(self):
            pytest.fail("scope failure acquired a connection")

    access = AccessContext(
        allowed_queue_ids=frozenset() if case == "empty" else frozenset(range(1, 34))
        if case == "limit" else frozenset({1}),
        selected_queue_id=2 if case == "forbidden" else None,
    )
    with pytest.raises(TriageError, match={
        "empty": "^tool access denied$", "forbidden": "^queue unavailable$",
        "limit": "^triage scope exceeds limit$",
    }[case]):
        triage_queues(NoConnection(), access, evaluated_at=NOW, policy=RiskPolicy())


def test_missing_and_unauthorized_selection_are_indistinguishable(triage_data):
    data = triage_data
    qid = data.queue()
    with statements(data.engine) as forbidden_sql:
        with pytest.raises(TriageError) as forbidden:
            data.run((qid,), selected=MISSING_ID)
    with statements(data.engine) as missing_sql:
        with pytest.raises(TriageError) as missing:
            data.run((MISSING_ID,), selected=MISSING_ID)
    assert str(forbidden.value) == str(missing.value) == "queue unavailable"
    assert not business_selects(forbidden_sql)
    assert len(business_selects(missing_sql)) == 1


@pytest.mark.parametrize("count", [1, 8, 32])
def test_constant_query_count_scalar_reads_and_32_queue_limit(triage_data, count):
    data = triage_data
    for _ in range(count):
        data.queue(tickets=(dict(created_at=NOW - timedelta(minutes=31)),))

    def no_entities(session, instance):
        pytest.fail("triage loaded an ORM business object")

    event.listen(read_only.ReadOnlySession, "loaded_as_persistent", no_entities)
    try:
        with statements(data.engine) as observed:
            result = data.run()
    finally:
        event.remove(read_only.ReadOnlySession, "loaded_as_persistent", no_entities)
    sql = business_selects(observed)
    assert len(sql) == 2 and len(result.rankings) == count
    assert all(r.operational_tie == (count > 1) for r in result.rankings)
    assert [r.assessment.queue_id for r in result.rankings] == data.ids
    assert result.detail_target_queue_id == data.ids[0]
    assert "count(" in sql[1]
    assert not any(private in " ".join(sql) for private in (
        "tickets.subject", "tickets.external_id", "tickets.priority", "agents.name",
    ))


def test_selected_scope_limit_applies_after_narrowing(triage_data):
    data = triage_data
    qid = data.queue()
    allowed = (qid, *range(MISSING_ID, MISSING_ID + 40))
    result = data.run(allowed, selected=qid)
    assert result.scope.effective_queue_ids == (qid,)
    assert result.available_queue_count == 1 and result.unavailable_queue_count == 0


@pytest.mark.parametrize("failure", ["none", "collector", "validation", "database"])
def test_sessions_close_transactions_rollback_and_rows_stay_unchanged(triage_data, monkeypatch, failure):
    data = triage_data
    data.queue()
    before = business_snapshot(data.engine)
    active, rollbacks = [], []
    original = read_only.ReadOnlySession

    class TrackingSession(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            active.append(self)

        def close(self):
            super().close()
            active.remove(self)

    def record_rollback(conn):
        rollbacks.append(True)

    monkeypatch.setattr(read_only, "ReadOnlySession", TrackingSession)
    build_result = queue_triage.build_triage_result

    def detached_build(*args):
        assert not active, "snapshot session remained open during pure evaluation/ranking"
        return build_result(*args)

    monkeypatch.setattr(queue_triage, "build_triage_result", detached_build)
    if failure != "none":
        collect = queue_triage.collect_triage_metrics

        def broken(*args):
            if failure == "collector":
                raise ValueError("PRIVATE failure")
            if failure == "database":
                raise OperationalError("PRIVATE SQL", {}, RuntimeError("PRIVATE driver error"))
            facts = collect(*args)
            return (replace(facts[0], metrics=replace(facts[0].metrics, active_tickets=True)),)

        monkeypatch.setattr(queue_triage, "collect_triage_metrics", broken)
    event.listen(data.engine, "rollback", record_rollback)
    try:
        if failure == "none":
            result = data.run()
            assert result.outcome == "no_triggered_rules"
        else:
            with pytest.raises(TriageError, match="^database unavailable$" if failure == "database"
                               else "^invalid triage data$"):
                data.run()
    finally:
        event.remove(data.engine, "rollback", record_rollback)
    assert not active and rollbacks
    assert business_snapshot(data.engine) == before
    with data.engine.connect() as conn:
        raw = conn.connection.driver_connection
        if data.engine.dialect.name == "sqlite":
            assert not raw.in_transaction
            assert conn.exec_driver_sql("PRAGMA query_only").scalar_one() == 0
        else:
            assert conn.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "off"
            assert conn.exec_driver_sql("SHOW transaction_isolation").scalar_one() == "read committed"


def test_no_single_queue_collector_or_ambient_configuration(triage_data, monkeypatch):
    from ai_operations_agent import assessment, config, db, metrics

    def blocked(*args, **kwargs):
        pytest.fail("triage used a single-queue collector or ambient settings")

    monkeypatch.setattr(metrics, "collect_queue_risk_metrics", blocked)
    monkeypatch.setattr(assessment, "collect_queue_risk_metrics", blocked)
    monkeypatch.setattr(db, "get_engine", blocked)
    monkeypatch.setattr(read_only, "get_engine", blocked)
    monkeypatch.setattr(config, "get_risk_policy", blocked)
    triage_data.queue()
    assert triage_data.run().available_queue_count == 1


def test_time_is_normalized_once_and_naive_times_fail_before_queries(triage_data):
    data = triage_data
    data.queue()
    offset = NOW.astimezone(timezone(timedelta(hours=-4)))
    assert data.run(evaluated_at=offset).evaluated_at == NOW
    with statements(data.engine) as observed:
        with pytest.raises(ValueError, match="timezone-aware"):
            data.run(evaluated_at=NOW.replace(tzinfo=None))
    assert not observed


@pytest.mark.parametrize("invalid_engine", [None, "sqlite+pysqlite:///:memory:"])
def test_engine_cannot_fall_back_to_ambient_settings(monkeypatch, invalid_engine):
    def blocked(*args, **kwargs):
        pytest.fail("triage attempted an ambient engine lookup")

    monkeypatch.setattr(read_only, "get_engine", blocked)
    with pytest.raises(TypeError, match="explicit Engine"):
        triage_queues(invalid_engine, AccessContext(allowed_queue_ids=frozenset({1})),
                      evaluated_at=NOW, policy=RiskPolicy())


def test_cutoff_overflow_is_sanitized_and_transaction_closes(triage_data):
    triage_data.queue()
    with pytest.raises(TriageError, match="^invalid triage data$"):
        triage_data.run(evaluated_at=datetime.min.replace(tzinfo=timezone.utc))


def test_canonical_assessment_compatibility_without_public_capability_changes(triage_data):
    from ai_operations_agent import queue_services, tools
    from ai_operations_agent.agent_schemas import AgentRequest, AgentResponse
    from ai_operations_agent.main import app

    data = triage_data
    qid = data.queue(online=0, tickets=(
        dict(created_at=NOW - timedelta(minutes=31), assigned=False),
    ))
    assessment = data.run().rankings[0].assessment
    with read_only.read_only_session(data.engine) as session:
        canonical = queue_services.risk_assessment_response(
            queue_services.assess_queue(session, qid, NOW, RiskPolicy()),
        )
    assert assessment.model_dump(mode="json") == canonical.model_dump(mode="json")
    assert tools.SUPPORTED_TOOLS == frozenset({"list_queues", "get_queue_recommendations"})
    assert "cross_queue_triage" not in str(AgentRequest.model_json_schema())
    assert AgentResponse.model_fields["schema_version"].default == "offline-agent-v1"
    assert not any("triage" in route.path for route in app.routes)
