"""Short-lived, agent-specific sessions. Never borrow an HTTP/fixture transaction."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_operations_agent.db import get_engine


class ReadOnlyViolation(RuntimeError):
    pass


class ReadOnlySession(Session):
    def flush(self, objects=None) -> None:
        raise ReadOnlyViolation("tool sessions cannot flush")

    def commit(self) -> None:
        raise ReadOnlyViolation("tool sessions cannot commit")


def _select_only(conn, cursor, statement, parameters, context, executemany):
    # Business code uses fixed SQLAlchemy SELECTs. Also guard raw SQL and bulk DML.
    if not statement.lstrip().upper().startswith("SELECT "):
        raise ReadOnlyViolation("tool sessions permit SELECT statements only")


@contextmanager
def read_only_session(
    engine: Engine | None = None, *, statement_timeout_ms: int = 5000,
) -> Iterator[Session]:
    engine = get_engine() if engine is None else engine
    if not isinstance(engine, Engine):
        raise TypeError("tool reads require an Engine, never an existing session or connection")
    if type(statement_timeout_ms) is not int or statement_timeout_ms <= 0:
        raise ValueError("statement timeout must be a positive integer")
    if engine.dialect.name not in {"postgresql", "sqlite"}:
        raise ValueError("unsupported tool database")
    with engine.connect() as connection:
        if engine.dialect.name == "postgresql":
            # An explicitly AUTOCOMMIT-configured Engine must not bypass transaction safety.
            connection = connection.execution_options(isolation_level="READ COMMITTED")
        transaction = connection.begin()
        previous_query_only = None
        guarded = False
        try:
            if engine.dialect.name == "postgresql":
                # First statements of our own transaction, before any business query.
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                connection.exec_driver_sql(f"SET LOCAL statement_timeout = {statement_timeout_ms}")
            else:
                previous_query_only = connection.exec_driver_sql("PRAGMA query_only").scalar_one()
                connection.exec_driver_sql("PRAGMA query_only = ON")
            event.listen(connection, "before_cursor_execute", _select_only)
            guarded = True
            with ReadOnlySession(bind=connection, autoflush=False) as session:
                yield session
        finally:
            try:
                if guarded:
                    event.remove(connection, "before_cursor_execute", _select_only)
                transaction.rollback()
                if previous_query_only is not None:
                    # SQLite's switch is connection-wide; restore it for pooled/in-memory users.
                    connection.exec_driver_sql(f"PRAGMA query_only = {previous_query_only}")
            except BaseException:
                # Failed cleanup must never return unknown transaction/PRAGMA state to the pool.
                connection.invalidate()
                raise
