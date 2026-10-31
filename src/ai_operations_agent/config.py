from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_operations_agent.risk import RiskPolicy


class Settings(BaseSettings):
    database_url: str
    risk_sla_remaining_percent: int = Field(default=20, ge=1, le=99)
    risk_unassigned_min_tickets: int = Field(default=1, ge=1)

    @field_validator("risk_sla_remaining_percent", "risk_unassigned_min_tickets", mode="before")
    @classmethod
    def reject_boolean_thresholds(cls, value):
        if isinstance(value, bool):
            raise ValueError("risk thresholds must be integers")
        return value

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()


def get_risk_policy() -> RiskPolicy:
    return RiskPolicy(settings.risk_sla_remaining_percent, settings.risk_unassigned_min_tickets)
