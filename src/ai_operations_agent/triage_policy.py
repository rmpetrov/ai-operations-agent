"""Pure proposed operational priority policy; no I/O or business-impact prediction."""

from ai_operations_agent.risk import RiskEvaluation, RiskMetrics

RANKING_POLICY_VERSION = "queue-triage-v1"
MAX_TRIAGE_QUEUES = 32
_SEVERITY = {"none": 0, "low": 1, "medium": 2, "high": 3}


def operational_key(
    metrics: RiskMetrics, evaluation: RiskEvaluation,
) -> tuple[int, int, int, int, int, int]:
    """Ascending lexicographic priority, excluding the technical queue-ID tie-break.

    Counts are compared separately, never added: these signals can overlap.
    Inputs are validated canonical assessments at the engine boundary.
    """
    codes = {signal.code for signal in evaluation.signals}
    return (
        -_SEVERITY[evaluation.overall_severity],
        -metrics.sla_breached_tickets,
        -metrics.sla_at_risk_tickets,
        -metrics.active_tickets if "NO_AGENTS_ONLINE" in codes else 0,
        -metrics.unassigned_tickets if "UNASSIGNED_BACKLOG" in codes else 0,
        -metrics.active_tickets,
    )
