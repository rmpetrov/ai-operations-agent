"""Offline v2 capture, independent grounding and trusted rendering. No database I/O."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import TypeAdapter, ValidationError

from ai_operations_agent.agent_evidence import GroundingError
from ai_operations_agent.agent_schemas import Identifier
from ai_operations_agent.queue_triage import effective_triage_scope
from ai_operations_agent.risk import RiskPolicy
from ai_operations_agent.tool_schemas import AccessContext
from ai_operations_agent.tools import EvaluationContext
from ai_operations_agent.triage_agent_schemas import (
    MAX_REFERENCES,
    MAX_RESPONSE_BYTES,
    MAX_SOURCE_BYTES,
    LinkedDetailSource,
    TriageAgentResponse,
    TriageEvidenceItem,
    TriageEvidenceSource,
    TriageExplanationBlock,
    TriageFailure,
    TriageReference,
    TriageSelection,
    TriageSource,
)
from ai_operations_agent.triage_schemas import QueueTriageResult

_SOURCE = TypeAdapter(TriageEvidenceSource)
_FAILURES = {
    "invalid_evidence": "answer failed evidence validation",
    "size_limit": "agent output exceeds size limit",
    "tool_unavailable": "tool unavailable",
    "access_denied": "tool access denied",
}
_INABILITY = {
    "needs_clarification": "Explicit trusted queue scope is required.",
    "insufficient_information": "Insufficient canonical evidence is available.",
    "unsupported": "This request requires an unsupported capability.",
    "error": "Cross-queue triage could not be completed.",
}
_UNCERTAINTY = (
    "Only the trusted effective scope contributes to this snapshot; unavailable queues are unassessed.",
    "Operational priority is advisory; overlapping counts must not be summed.",
    "Recorded presence is not capacity; this snapshot is not a forecast or health guarantee.",
)


def _json(value) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def _check_size(payload: str, maximum: int):
    if len(payload.encode("utf-8")) > maximum:
        raise GroundingError("evidence exceeds size limit")


def _canonical_sources(sources, run_id):
    """Reparse even constructed/copied instances; reject rather than repair sources."""
    if not 1 <= len(sources) <= 2:
        raise GroundingError("invalid source count")
    validated = []
    for source in sources:
        if type(source) not in {TriageSource, LinkedDetailSource}:
            raise GroundingError("unsupported source")
        source = _SOURCE.validate_python(source, strict=True)
        payload = source.model_dump_json()
        _check_size(payload, MAX_SOURCE_BYTES)
        validated.append(_SOURCE.validate_json(payload, strict=True))
    ids = [s.source_id for s in validated]
    if len(ids) != len(set(ids)) or run_id in ids or any(s.run_id != run_id for s in validated):
        raise GroundingError("invalid source provenance")
    triage = validated[0]
    if type(triage) is not TriageSource:
        raise GroundingError("missing parent triage source")
    if len(validated) == 2:
        detail = validated[1]
        if (
            type(detail) is not LinkedDetailSource
            or detail.parent_triage_source_id != triage.source_id
        ):
            raise GroundingError("invalid parent triage source")
        target = triage.snapshot.summary.detail_target_queue_id
        if target is None or detail.snapshot.assessment.queue_id != target:
            raise GroundingError("invalid detail target")
        assessment = next(
            row.assessment for row in triage.snapshot.queues if row.assessment.queue_id == target
        )
        if detail.snapshot.assessment.model_dump(mode="json") != assessment.model_dump(mode="json"):
            raise GroundingError("different detail assessment")
    return tuple(validated)


def canonical_triage_evidence(sources, run_id) -> tuple[TriageEvidenceItem, ...]:
    sources = _canonical_sources(sources, run_id)
    items = []
    for source in sources:
        snapshot = source.snapshot.model_dump(mode="json")
        if type(source) is TriageSource:
            entries = [("triage_summary", "/summary", None, snapshot["summary"])]
            entries += [
                ("triage_queue", f"/queues/{i}", row["assessment"]["queue_id"], row)
                for i, row in enumerate(snapshot["queues"])
            ]
        else:
            qid = snapshot["assessment"]["queue_id"]
            entries = [("assessment", "/assessment", qid, snapshot["assessment"])]
            entries += [
                ("recommendation", f"/recommendations/{i}", qid, recommendation)
                for i, recommendation in enumerate(snapshot["recommendations"])
            ]
        for kind, pointer, qid, value in entries:
            items.append(
                TriageEvidenceItem(
                    reference=TriageReference(
                        source_id=source.source_id, kind=kind, pointer=pointer
                    ),
                    queue_id=qid,
                    value=value,
                )
            )
    if len(items) > MAX_REFERENCES:
        raise GroundingError("too many evidence references")
    return tuple(items)


def canonical_triage_blocks(sources, evidence):
    """Fixed trusted templates, with no free-form claims or model-authored rankings."""
    triage = sources[0]
    summary = triage.snapshot.summary
    summary_ref = (evidence[0].reference,)
    blocks = [
        TriageExplanationBlock(
            kind="triage_summary",
            references=summary_ref,
            text=(
                f"Authorized scope: {len(summary.scope.effective_queue_ids)} queues; "
                f"available: {summary.available_queue_count}; unavailable: {summary.unavailable_queue_count}; "
                f"eligible for detail: {summary.eligible_queue_count}. "
                f"Evaluated at {summary.evaluated_at.isoformat()}; ranking policy {summary.ranking_policy_version}."
            ),
        )
    ]
    for row, item in zip(
        triage.snapshot.queues, evidence[1 : 1 + len(triage.snapshot.queues)], strict=True
    ):
        a = row.assessment
        blocks.append(
            TriageExplanationBlock(
                kind="ranked_queue",
                references=(item.reference,),
                text=(
                    f"Position {row.position}: {a.queue_name} (queue {a.queue_id}); "
                    f"severity {a.overall_severity}; active tickets {a.metrics.active_tickets}; "
                    f"operational tie: {'yes' if row.operational_tie else 'no'}; "
                    f"detail eligible: {'yes' if row.detail_eligible else 'no'}."
                ),
            )
        )
    if summary.outcome == "no_available_queues":
        blocks.append(
            TriageExplanationBlock(
                kind="no_available_queues",
                references=summary_ref,
                text="No queues in the effective scope are available for assessment.",
            )
        )
    elif summary.outcome == "no_triggered_rules":
        blocks.append(
            TriageExplanationBlock(
                kind="no_rules",
                references=summary_ref,
                text="No configured risk rule triggered. This does not guarantee operational health.",
            )
        )
    if len(sources) == 2:
        detail = sources[1]
        detail_items = evidence[1 + len(triage.snapshot.queues) :]
        assessment_ref = detail_items[0].reference
        for recommendation, item in zip(
            detail.snapshot.recommendations, detail_items[1:], strict=True
        ):
            blocks.append(
                TriageExplanationBlock(
                    kind="recommendations",
                    references=(assessment_ref, item.reference),
                    text=(
                        f"{recommendation.code} [{recommendation.priority}]: "
                        f"{recommendation.suggested_action} {recommendation.explanation}"
                    ),
                )
            )
    return tuple(blocks)


def _validate_triage_response_contract(response):
    """Semantic check invoked after the strict structural Pydantic validation."""
    try:
        _check_size(response.model_dump_json(), MAX_RESPONSE_BYTES)
        if response.status != "answered":
            # A failure carries no snapshots or partial operational conclusions.
            if response.sources or response.evidence or response.uncertainty:
                raise GroundingError("operational conclusions after failure")
            expected = (
                TriageExplanationBlock(
                    kind="inability", references=(), text=_INABILITY[response.status]
                ),
            )
            if response.explanation != expected:
                raise GroundingError("invalid inability explanation")
            if response.status == "error":
                if (
                    response.error is None
                    or response.error.code not in _FAILURES
                    or response.error.message != _FAILURES[response.error.code]
                ):
                    raise GroundingError("invalid sanitized error")
            elif response.error is not None:
                raise GroundingError("inconsistent failure status")
            return
        if response.error is not None:
            raise GroundingError("inconsistent failure status")
        sources = _canonical_sources(response.sources, response.run_id)
        expected = canonical_triage_evidence(sources, response.run_id)
        # JSON equality is type-sensitive (Python would equate True, 1 and 1.0).
        if _json([item.model_dump(mode="json") for item in response.evidence]) != _json(
            [item.model_dump(mode="json") for item in expected]
        ):
            raise GroundingError("missing or noncanonical evidence")
        if (
            response.explanation != canonical_triage_blocks(sources, expected)
            or response.uncertainty != _UNCERTAINTY
        ):
            raise GroundingError("noncanonical explanation")
    except GroundingError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError):
        raise GroundingError("invalid triage evidence") from None


def validate_triage_response(response):
    """Complete independent check, including constructed/copied models and safety flags.

    The model's after-validator calls only the semantic checker above, avoiding recursion.
    Capture provenance additionally requires the private ledger comparison.
    """
    try:
        if type(response) is not TriageAgentResponse:
            raise GroundingError("invalid triage response")
        _check_size(response.model_dump_json(), MAX_RESPONSE_BYTES)
        TriageAgentResponse.model_validate(response, strict=True)
    except GroundingError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError):
        raise GroundingError("invalid triage response") from None


class TriageEvidenceLedger:
    """Trusted offline capture only. Does not dispatch, continue runs or re-assess data."""

    def __init__(
        self,
        run_id: str,
        access: AccessContext,
        evaluation_context: EvaluationContext,
        new_id: Callable[[], str],
    ):
        try:
            self.run_id = TypeAdapter(Identifier).validate_python(run_id, strict=True)
            if (
                type(access) is not AccessContext
                or type(evaluation_context) is not EvaluationContext
            ):
                raise ValueError
            self._access = AccessContext.model_validate(access.model_dump(), strict=True)
            if "get_queue_triage" not in self._access.allowed_tool_names:
                raise ValueError
            self._scope = effective_triage_scope(self._access)
            when, policy = evaluation_context.evaluated_at, evaluation_context.policy
            if (
                type(when) is not datetime
                or when.utcoffset() != timedelta(0)
                or type(policy) is not RiskPolicy
            ):
                raise ValueError
            self._when = when
            self._policy = RiskPolicy(policy.sla_remaining_percent, policy.unassigned_min_tickets)
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise GroundingError("invalid trusted triage context") from None
        self._new_id = new_id
        # Store private serialized values, never caller-owned dictionaries or models.
        self._sources: list[str] = []

    @property
    def sources(self):
        return tuple(_SOURCE.validate_json(payload, strict=True) for payload in self._sources)

    def capture(self, name: str, snapshot: dict, *, parent_triage_source_id: str | None = None):
        """Capture detached canonical synthetic/tool results. No extra reads are authorized."""
        try:
            if type(name) is not str or type(snapshot) is not dict:
                raise GroundingError("unsupported source")
            payload = _json(snapshot)
            _check_size(payload, min(MAX_SOURCE_BYTES, self._access.limits.max_result_bytes))
            if name == "get_queue_triage":
                if self._sources or parent_triage_source_id is not None:
                    raise GroundingError("duplicate triage source")
                result = QueueTriageResult.model_validate_json(payload, strict=True)
                if (
                    result.scope != self._scope
                    or result.evaluated_at != self._when
                    or result.risk_policy.risk_policy() != self._policy
                ):
                    raise GroundingError("source outside trusted context")
                summary = result.model_dump(mode="json")
                queues = summary.pop("rankings")
                data = dict(tool_name=name, snapshot=dict(summary=summary, queues=queues))
            elif name == "get_queue_recommendations":
                if (
                    len(self._sources) != 1
                    or "get_queue_recommendations" not in self._access.allowed_tool_names
                ):
                    raise GroundingError("invalid linked source")
                data = dict(
                    tool_name=name,
                    snapshot=json.loads(payload),
                    parent_triage_source_id=parent_triage_source_id,
                )
            else:
                raise GroundingError("unsupported source")
            source = _SOURCE.validate_json(
                _json(
                    dict(
                        schema_version="triage-evidence-v2",
                        source_id=self._new_id(),
                        run_id=self.run_id,
                        **data,
                    )
                ),
                strict=True,
            )
            _check_size(source.model_dump_json(), MAX_SOURCE_BYTES)
            _canonical_sources((*self.sources, source), self.run_id)
            self._sources.append(source.model_dump_json())  # atomic, only after complete validation
            return source
        except GroundingError:
            raise
        except (ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError):
            raise GroundingError("invalid triage source") from None

    def references(self):
        return tuple(
            item.reference for item in canonical_triage_evidence(self.sources, self.run_id)
        )

    def validate(self, response):
        """Private provenance equality plus independent validation; never repairs a response."""
        validate_triage_response(response)
        if response.run_id != self.run_id:
            raise GroundingError("wrong run")
        if response.status == "answered" and tuple(
            s.model_dump_json() for s in response.sources
        ) != tuple(self._sources):
            raise GroundingError("source differs from private ledger")
        _check_size(response.model_dump_json(), MAX_RESPONSE_BYTES)


def build_triage_answer(ledger: TriageEvidenceLedger, selection: TriageSelection | None = None):
    """Pure trusted test builder; deliberately absent from run_agent and model adapters."""
    try:
        sources = ledger.sources
        evidence = canonical_triage_evidence(sources, ledger.run_id)
        expected = tuple(item.reference for item in evidence)
        if selection is not None:
            selection = TriageSelection.model_validate(selection, strict=True)
            if selection.references != expected:
                raise GroundingError("invalid evidence selection")
        response = TriageAgentResponse(
            run_id=ledger.run_id,
            status="answered",
            sources=sources,
            evidence=evidence,
            explanation=canonical_triage_blocks(sources, evidence),
            uncertainty=_UNCERTAINTY,
        )
        ledger.validate(response)
        return response
    except (GroundingError, ValidationError, TypeError, ValueError, AttributeError, RecursionError):
        # No partial response, retries, repair, fresh assessment, or alternate execution.
        raise GroundingError("invalid triage answer") from None


def build_triage_inability(run_id: str, status="error", *, code="invalid_evidence"):
    if status not in _INABILITY or (status == "error" and code not in _FAILURES):
        raise GroundingError("invalid inability")
    return TriageAgentResponse(
        run_id=run_id,
        status=status,
        explanation=(
            TriageExplanationBlock(kind="inability", text=_INABILITY[status], references=()),
        ),
        error=TriageFailure(code=code, message=_FAILURES[code]) if status == "error" else None,
    )
