"""Pure queue risk rules: no database, HTTP, or clock access."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

Severity = Literal["none", "low", "medium", "high"]
SignalSeverity = Literal["low", "medium", "high"]
SignalCode = Literal["SLA_BREACHED", "SLA_AT_RISK", "UNASSIGNED_BACKLOG", "NO_AGENTS_ONLINE"]


@dataclass(frozen=True)
class RiskPolicy:
    sla_remaining_percent: int = 20
    unassigned_min_tickets: int = 1

    def __post_init__(self) -> None:
        if type(self.sla_remaining_percent) is not int or not 1 <= self.sla_remaining_percent <= 99:
            raise ValueError("sla_remaining_percent must be an integer from 1 to 99")
        if type(self.unassigned_min_tickets) is not int or self.unassigned_min_tickets < 1:
            raise ValueError("unassigned_min_tickets must be a positive integer")

    def warning_window(self, sla_minutes: int) -> timedelta:
        # Integer microseconds preserve fractional seconds without minute rounding.
        return timedelta(microseconds=sla_minutes * self.sla_remaining_percent * 600_000)


@dataclass(frozen=True)
class RiskMetrics:
    active_tickets: int
    unassigned_tickets: int
    sla_breached_tickets: int
    sla_at_risk_tickets: int
    online_agents: int
    future_created_active_tickets: int


@dataclass(frozen=True)
class RiskSignal:
    code: SignalCode
    severity: SignalSeverity
    affected_tickets: int
    reason: str


@dataclass(frozen=True)
class RiskEvaluation:
    overall_severity: Severity
    signals: tuple[RiskSignal, ...]


def evaluate_risk(
    metrics: RiskMetrics,
    policy: RiskPolicy,
    warning_window_seconds: float,
) -> RiskEvaluation:
    """Return triggered signals in policy order, with maximum (never summed) severity."""
    signals = []
    if metrics.sla_breached_tickets > 0:
        signals.append(
            RiskSignal(
                "SLA_BREACHED",
                "high",
                metrics.sla_breached_tickets,
                (
                    "1 active ticket is past its SLA deadline."
                    if metrics.sla_breached_tickets == 1
                    else f"{metrics.sla_breached_tickets} active tickets are past their SLA deadline."
                ),
            )
        )
    if metrics.sla_at_risk_tickets > 0:
        signals.append(
            RiskSignal(
                "SLA_AT_RISK",
                "medium",
                metrics.sla_at_risk_tickets,
                f"{metrics.sla_at_risk_tickets} active "
                f"{'ticket is' if metrics.sla_at_risk_tickets == 1 else 'tickets are'} "
                "due now or within "
                f"{warning_window_seconds:g} seconds.",
            )
        )
    if metrics.unassigned_tickets >= policy.unassigned_min_tickets:
        signals.append(
            RiskSignal(
                "UNASSIGNED_BACKLOG",
                "low",
                metrics.unassigned_tickets,
                (
                    "1 active ticket has no assigned agent."
                    if metrics.unassigned_tickets == 1
                    else f"{metrics.unassigned_tickets} active tickets have no assigned agent."
                ),
            )
        )
    if metrics.active_tickets > 0 and metrics.online_agents == 0:
        signals.append(
            RiskSignal(
                "NO_AGENTS_ONLINE",
                "medium",
                metrics.active_tickets,
                f"No agents are recorded online for this queue's {metrics.active_tickets} active "
                f"{'ticket' if metrics.active_tickets == 1 else 'tickets'}.",
            )
        )
    severity_order = {"none": 0, "low": 1, "medium": 2, "high": 3}
    overall = max(
        (signal.severity for signal in signals), key=severity_order.__getitem__, default="none"
    )
    return RiskEvaluation(overall, tuple(signals))
