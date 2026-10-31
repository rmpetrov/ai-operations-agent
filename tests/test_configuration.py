import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import URL

from ai_operations_agent.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_database_url_is_required_without_environment_or_dotenv(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)
    assert exc_info.value.errors()[0]["loc"] == ("database_url",)
    assert exc_info.value.errors()[0]["type"] == "missing"


def test_database_url_can_be_loaded_from_dotenv(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    url = "postgresql+psycopg://dotenv.invalid/configuration_test"
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"DATABASE_URL={url}\n")
    assert Settings(_env_file=dotenv).database_url == url


def test_database_url_environment_overrides_dotenv(monkeypatch, tmp_path):
    url = "postgresql+psycopg://environment.invalid/configuration_test"
    monkeypatch.setenv("DATABASE_URL", url)
    dotenv = tmp_path / ".env"
    dotenv.write_text("DATABASE_URL=postgresql+psycopg://dotenv.invalid/configuration_test\n")
    assert Settings(_env_file=dotenv).database_url == url


def test_alembic_offline_migration_uses_environment_configuration():
    # Generate credentials only for this process; no database connection is opened.
    url = URL.create(
        "postgresql+psycopg",
        username=secrets.token_hex(8),
        password=f"{secrets.token_urlsafe(16)}%",
        host="database.invalid",
        database="configuration_test",
    ).render_as_string(hide_password=False)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE tickets" in result.stdout
    assert "CREATE TABLE queues" in result.stdout


def test_api_startup_requires_explicit_database_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = subprocess.run(
        [sys.executable, "-c", "from ai_operations_agent.main import app"],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "database_url" in result.stderr
    assert "Field required" in result.stderr


@pytest.mark.parametrize(
    ("field", "value"),
    [("risk_sla_remaining_percent", value) for value in (0, 100, -1, 20.5, True, "bad")]
    + [("risk_unassigned_min_tickets", value) for value in (0, -1, 1.5, True, "bad")],
)
def test_invalid_risk_configuration(field, value):
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, database_url="postgresql+psycopg://config.invalid/ops",
                 **{field: value})
    assert exc_info.value.errors()[0]["loc"] == (field,)


def test_risk_configuration_from_environment(monkeypatch):
    monkeypatch.setenv("RISK_SLA_REMAINING_PERCENT", "35")
    monkeypatch.setenv("RISK_UNASSIGNED_MIN_TICKETS", "3")
    configured = Settings(_env_file=None, database_url="postgresql+psycopg://config.invalid/ops")
    assert configured.risk_sla_remaining_percent == 35
    assert configured.risk_unassigned_min_tickets == 3


def test_default_risk_configuration(monkeypatch):
    monkeypatch.delenv("RISK_SLA_REMAINING_PERCENT", raising=False)
    monkeypatch.delenv("RISK_UNASSIGNED_MIN_TICKETS", raising=False)
    configured = Settings(_env_file=None, database_url="postgresql+psycopg://config.invalid/ops")
    assert configured.risk_sla_remaining_percent == 20
    assert configured.risk_unassigned_min_tickets == 1
