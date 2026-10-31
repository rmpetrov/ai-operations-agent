"""Pure advisory recommendation catalog: no database, clock, HTTP, or other I/O."""

from ai_operations_agent.risk import RiskEvaluation, RiskMetrics, RiskPolicy, SignalCode
from ai_operations_agent.schemas import (
    ActionRecommendation,
    OverdueEvidence,
    RecordedAgentStatusEvidence,
    ReviewOverdueTickets,
    ReviewUpcomingDeadlines,
    TriageUnassignedTickets,
    UnassignedEvidence,
    UpcomingDeadlineEvidence,
    VerifyRecordedAgentStatus,
)

# Explicit priority order and medium-priority tie break; independent of signal input order.
RECOMMENDATION_SIGNAL_ORDER: tuple[SignalCode, ...] = (
    "SLA_BREACHED", "SLA_AT_RISK", "NO_AGENTS_ONLINE", "UNASSIGNED_BACKLOG",
)


def build_recommendations(
    metrics: RiskMetrics,
    policy: RiskPolicy,
    warning_window_seconds: float,
    evaluation: RiskEvaluation,
) -> tuple[ActionRecommendation, ...]:
    signals = {}
    for signal in evaluation.signals:
        if signal.code in signals and signals[signal.code] != signal:
            raise ValueError("conflicting duplicate risk signal")
        signals[signal.code] = signal
    recommendations: list[ActionRecommendation] = []
    for code in RECOMMENDATION_SIGNAL_ORDER:
        if code not in signals:
            continue
        reason = signals[code].reason
        match code:
            case "SLA_BREACHED":
                recommendations.append(ReviewOverdueTickets(
                    suggested_action="Review overdue active tickets and decide the next response.",
                    explanation=reason,
                    evidence=OverdueEvidence(sla_breached_tickets=metrics.sla_breached_tickets),
                ))
            case "SLA_AT_RISK":
                recommendations.append(ReviewUpcomingDeadlines(
                    suggested_action="Review active tickets due now or within the warning window.",
                    explanation=reason,
                    evidence=UpcomingDeadlineEvidence(
                        sla_at_risk_tickets=metrics.sla_at_risk_tickets,
                        sla_at_risk_window_seconds=warning_window_seconds,
                    ),
                ))
            case "NO_AGENTS_ONLINE":
                recommendations.append(VerifyRecordedAgentStatus(
                    suggested_action="Verify recorded agent status and confirm actual availability.",
                    explanation=f"{reason} Recorded status does not establish actual availability.",
                    evidence=RecordedAgentStatusEvidence(
                        active_tickets=metrics.active_tickets, online_agents=metrics.online_agents,
                    ),
                ))
            case "UNASSIGNED_BACKLOG":
                recommendations.append(TriageUnassignedTickets(
                    suggested_action=(
                        "Review unassigned active tickets and decide whether assignment is appropriate."
                    ),
                    explanation=reason,
                    evidence=UnassignedEvidence(
                        unassigned_tickets=metrics.unassigned_tickets,
                        unassigned_backlog_min_tickets=policy.unassigned_min_tickets,
                    ),
                ))
    return tuple(recommendations)
