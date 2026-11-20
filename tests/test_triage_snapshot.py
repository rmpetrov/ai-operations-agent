"""Native read-only enforcement and interleaved writers on independent connections."""

from contextlib import closing
from datetime import timedelta
from sqlite3 import OperationalError as SQLiteOperationalError

import pytest
from psycopg.errors import ReadOnlySqlTransaction
from sqlalchemy import create_engine, event, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_queue_triage import NOW, OwnedQueues, business_selects, statements
from test_queue_triage import triage_data as triage_data

from ai_operations_agent.evaluation_fixtures import business_snapshot
from ai_operations_agent.models import Agent, Base, Queue, Ticket
from ai_operations_agent.read_only import ReadOnlyViolation, read_only_session


@pytest.mark.parametrize("operation", ["flush", "commit", "orm", "core", "raw", "ddl", "native"])
def test_snapshot_blocks_writes_and_keeps_all_business_rows(triage_data, operation):
    data = triage_data
    qid = data.queue(tickets=(dict(created_at=NOW),))
    before = business_snapshot(data.engine)
    with read_only_session(data.engine, snapshot=True) as db:
        assert not db.autoflush
        db.add(Queue(name="Pending value must never flush", sla_minutes=1))
        assert db.execute(select(Queue.id).where(Queue.id == qid)).scalar_one() == qid
        if operation == "native":
            raw = db.connection().connection.driver_connection
            expected = ReadOnlySqlTransaction if data.engine.dialect.name == "postgresql" \
                else SQLiteOperationalError
            with closing(raw.cursor()) as cursor:
                with pytest.raises(expected) as rejected:
                    cursor.execute("UPDATE queues SET sla_minutes = 1")
            if data.engine.dialect.name == "postgresql":
                assert rejected.value.sqlstate == "25006"
        else:
            with pytest.raises(ReadOnlyViolation):
                if operation == "flush":
                    db.flush()
                elif operation == "commit":
                    db.commit()
                elif operation == "orm":
                    db.execute(update(Queue).where(Queue.id == qid).values(sla_minutes=1))
                elif operation == "core":
                    db.connection().execute(Queue.__table__.delete().where(Queue.id == qid))
                elif operation == "raw":
                    db.connection().exec_driver_sql("UPDATE queues SET sla_minutes = 1")
                else:
                    db.execute(text("CREATE TABLE forbidden_triage_table (id INTEGER)"))
    assert business_snapshot(data.engine) == before


@pytest.mark.parametrize("snapshot", [False, True])
def test_native_mode_precedes_business_reads_and_default_is_unchanged(triage_data, snapshot):
    data = triage_data
    qid = data.queue()
    with statements(data.engine) as observed:
        with read_only_session(data.engine, snapshot=snapshot, statement_timeout_ms=1234) as db:
            raw = db.connection().connection.driver_connection
            if data.engine.dialect.name == "postgresql":
                with closing(raw.cursor()) as cursor:
                    cursor.execute("SHOW transaction_isolation")
                    assert cursor.fetchone()[0] == ("repeatable read" if snapshot else "read committed")
                    cursor.execute("SHOW transaction_read_only")
                    assert cursor.fetchone()[0] == "on"
                    cursor.execute("SHOW statement_timeout")
                    assert cursor.fetchone()[0] == "1234ms"
            else:
                if snapshot:
                    assert raw.in_transaction  # Before the first SELECT, not just after it.
                assert raw.execute("PRAGMA query_only").fetchone()[0] == 1
            assert db.execute(select(Queue.id).where(Queue.id == qid)).scalar_one() == qid
    assert len(business_selects(observed)) == 1
    if data.engine.dialect.name == "sqlite":
        assert not raw.in_transaction
        assert ("BEGIN" in observed) == snapshot
        assert observed.index("PRAGMA query_only = ON") < observed.index(business_selects(observed)[0])
    else:
        assert observed[:2] == ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = 1234"]
        with data.engine.connect() as conn:
            assert conn.exec_driver_sql("SHOW transaction_isolation").scalar_one() == "read committed"
            assert conn.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "off"


def interleaved_update(data, *, sqlite=False):
    """Writer commits after the metadata SELECT, before the batch aggregate SELECT."""
    qid = data.ids[0]
    fired, read_transactions = [], []

    def after_query(conn, cursor, statement, params, context, executemany):
        if (fired or not statement.lstrip().upper().startswith("SELECT ") or
                "FROM queues" not in statement):
            return
        fired.append(True)
        if sqlite:
            read_transactions.append(conn.connection.driver_connection.in_transaction)
        with data.engine.begin() as writer:
            writer.execute(update(Queue).where(Queue.id == qid).values(sla_minutes=60))
            writer.execute(update(Agent).where(Agent.queue_id == qid).values(status="offline"))
            writer.execute(update(Ticket).where(Ticket.queue_id == qid).values(
                created_at=NOW - timedelta(minutes=5),
            ))

    event.listen(data.engine, "after_cursor_execute", after_query)
    try:
        old = data.run().rankings[0].assessment
    finally:
        event.remove(data.engine, "after_cursor_execute", after_query)
    assert fired == [True]
    assert old.sla_minutes == 30 and old.policy.sla_at_risk_window_seconds == 360
    assert old.metrics.sla_breached_tickets == old.metrics.online_agents == 1
    assert old.overall_severity == "high"
    # A later operation sees all three committed changes, with the identical evaluation time.
    fresh = data.run().rankings[0].assessment
    assert fresh.sla_minutes == 60 and fresh.policy.sla_at_risk_window_seconds == 720
    assert fresh.metrics.sla_breached_tickets == fresh.metrics.online_agents == 0
    assert fresh.overall_severity == "medium"
    if sqlite:
        assert read_transactions == [True]


def test_postgresql_concurrent_update_uses_one_snapshot(triage_data):
    if triage_data.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL REPEATABLE READ verification requires TEST_DATABASE_URL")
    triage_data.queue(tickets=(dict(created_at=NOW - timedelta(minutes=31)),))
    interleaved_update(triage_data)


def test_sqlite_real_transaction_is_consistent_across_an_interleaved_writer(tmp_path):
    # WAL allows the independent writer to commit while the read snapshot is still open.
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'triage-snapshot.db'}")
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode = WAL").scalar_one() == "wal"
        Base.metadata.create_all(engine)
        data = OwnedQueues(engine)
        data.queue(tickets=(dict(created_at=NOW - timedelta(minutes=31)),))
        interleaved_update(data, sqlite=True)
    finally:
        engine.dispose()


@pytest.mark.parametrize("initial", [0, 1])
def test_sqlite_snapshot_restores_previous_query_only_setting(triage_data, initial):
    if triage_data.engine.dialect.name != "sqlite":
        pytest.skip("SQLite PRAGMA restoration")
    data = triage_data
    data.queue()
    try:
        with data.engine.connect() as conn:
            conn.exec_driver_sql(f"PRAGMA query_only = {initial}")
        data.run()
        with data.engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA query_only").scalar_one() == initial
            assert not conn.connection.driver_connection.in_transaction
    finally:
        with data.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA query_only = 0")


def test_sqlite_begin_failure_restores_mode_and_closes_transaction(triage_data):
    if triage_data.engine.dialect.name != "sqlite":
        pytest.skip("SQLite explicit BEGIN failure")
    data = triage_data
    data.queue()

    def fail_begin(conn, cursor, statement, params, context, executemany):
        if statement == "BEGIN":
            raise OperationalError("PRIVATE begin", {}, RuntimeError("PRIVATE error"))

    event.listen(data.engine, "before_cursor_execute", fail_begin)
    try:
        from ai_operations_agent.queue_triage import TriageError

        with pytest.raises(TriageError, match="^database unavailable$"):
            data.run()
    finally:
        event.remove(data.engine, "before_cursor_execute", fail_begin)
    with data.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA query_only").scalar_one() == 0
        assert not conn.connection.driver_connection.in_transaction


def test_snapshot_mode_does_not_accept_an_existing_session_or_nonboolean(triage_data):
    data = triage_data
    data.queue()
    with Session(data.engine) as session:
        with pytest.raises(TypeError, match="Engine"):
            with read_only_session(session, snapshot=True):
                pytest.fail("borrowed an active session")
    with statements(data.engine) as observed:
        with pytest.raises(ValueError, match="boolean"):
            with read_only_session(data.engine, snapshot="true"):
                pytest.fail("coerced mode")
    assert not observed


def test_postgresql_autocommit_engine_cannot_bypass_snapshot_mode(triage_data):
    if triage_data.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL AUTOCOMMIT isolation override")
    with read_only_session(
        triage_data.engine.execution_options(isolation_level="AUTOCOMMIT"), snapshot=True,
    ) as db:
        raw = db.connection().connection.driver_connection
        with closing(raw.cursor()) as cursor:
            cursor.execute("SHOW transaction_isolation")
            assert cursor.fetchone()[0] == "repeatable read"
            cursor.execute("SHOW transaction_read_only")
            assert cursor.fetchone()[0] == "on"
