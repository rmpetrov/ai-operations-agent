import secrets

import pytest
from conftest import engine, pytest_configure, validate_test_database_url
from sqlalchemy.engine import URL

DEVELOPMENT_URL = str(URL.create("postgresql+psycopg", host="localhost", port=5433,
                                 database="ai_operations"))
TEST_URL = str(URL.create("postgresql+psycopg", host="127.0.0.1", port=5434,
                         database="ai_operations_test"))


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "valid PostgreSQL URL"),
        ("not-a-url", "valid PostgreSQL URL"),
        ("postgresql://localhost:invalid/ai_operations_test", "valid PostgreSQL URL"),
        ("sqlite+pysqlite:///:memory:", "must use PostgreSQL"),
        ("sqlite+pysqlite:///development.db", "must use PostgreSQL"),
        (DEVELOPMENT_URL, "configured development database"),
        # Different host spelling, port, or driver cannot bypass the database-name check.
        ("postgresql://127.0.0.1:5434/ai_operations", "configured development database"),
        ("postgresql://localhost:5434/postgres", "dedicated database ending in '_test'"),
        ("postgresql://localhost:5434/ai_operations_test_backup", "ending in '_test'"),
        ("postgresql://localhost:5434/", "ending in '_test'"),
        ("postgresql:///ai_operations_test", "explicit host"),
        (f"{TEST_URL}?dbname=ai_operations", "connection query parameters"),
        (f"{TEST_URL}?host=localhost&port=5433", "connection query parameters"),
        (f"{TEST_URL}?service=development", "connection query parameters"),
    ],
)
def test_unsafe_test_database_fails_before_engine_creation(monkeypatch, value, message):
    monkeypatch.setenv("TEST_DATABASE_URL", value)
    monkeypatch.setattr("conftest.settings.database_url", DEVELOPMENT_URL)

    def unexpected_engine(*args, **kwargs):
        pytest.fail("unsafe configuration reached create_engine")

    monkeypatch.setattr("conftest.create_engine", unexpected_engine)
    with pytest.raises(pytest.UsageError, match=message):
        pytest_configure(None)
    with pytest.raises(ValueError, match=message):
        next(engine.__wrapped__())


def test_dedicated_test_database_url_is_accepted():
    url = validate_test_database_url(TEST_URL, DEVELOPMENT_URL)
    assert url.get_backend_name() == "postgresql"
    assert url.host == "127.0.0.1"
    assert url.port == 5434
    assert url.database == "ai_operations_test"


def test_configured_development_database_with_test_suffix_is_rejected():
    with pytest.raises(ValueError, match="configured development database"):
        validate_test_database_url(TEST_URL, TEST_URL.replace("127.0.0.1", "localhost"))


def test_configuration_errors_do_not_expose_credentials():
    username = secrets.token_hex(8)
    password = secrets.token_urlsafe(16)
    value = URL.create("postgresql", username=username, password=password,
                       host="localhost", database="ai_operations").render_as_string(
                           hide_password=False)
    with pytest.raises(ValueError) as exc_info:
        validate_test_database_url(value, DEVELOPMENT_URL)
    assert password not in str(exc_info.value)
    assert username not in str(exc_info.value)


def test_unset_test_database_keeps_sqlite_default(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    pytest_configure(None)
    fixture = engine.__wrapped__()
    try:
        assert next(fixture).dialect.name == "sqlite"
    finally:
        fixture.close()
