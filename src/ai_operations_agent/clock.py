"""One injectable evaluation timestamp for each assessment request."""

from datetime import datetime, timezone

from ai_operations_agent.models import utc_now


def normalize_evaluation_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evaluation time must be timezone-aware")
    return value.astimezone(timezone.utc)


def get_evaluation_time() -> datetime:
    return normalize_evaluation_time(utc_now())
