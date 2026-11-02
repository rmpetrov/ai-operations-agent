"""Read-only future cost estimates. No price defaults, billing calls, or spending caps."""

from datetime import date
from decimal import Decimal

from pydantic import Field, field_validator

from ai_operations_agent.agent_telemetry import TokenUsage
from ai_operations_agent.tool_schemas import StrictValue


class VerifiedPriceTable(StrictValue):
    version: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    source_url: str = Field(pattern=r"^https://")
    externally_verified_on: date
    usd_per_million_input_tokens: Decimal = Field(ge=0, allow_inf_nan=False)
    usd_per_million_output_tokens: Decimal = Field(ge=0, allow_inf_nan=False)

    @field_validator("externally_verified_on")
    @classmethod
    def no_future_verification(cls, value):
        if value > date.today():
            raise ValueError("verification cannot be in the future")
        return value


def estimate_cost(usage: TokenUsage, model_id: str | None,
                  prices: VerifiedPriceTable | None = None) -> dict:
    amount = None
    if (prices is not None and model_id == prices.model_id
            and usage.input_tokens is not None and usage.output_tokens is not None):
        amount = str((usage.input_tokens * prices.usd_per_million_input_tokens
                      + usage.output_tokens * prices.usd_per_million_output_tokens) / 1_000_000)
    return {"estimated_usd": amount, "pricing_version": prices.version if prices else None,
            "pricing_source": prices.source_url if prices else None,
            "externally_verified_on": str(prices.externally_verified_on) if prices else None,
            "label": "Estimate only; not a provider billing cap. Unknown without usage and verified pricing."}
