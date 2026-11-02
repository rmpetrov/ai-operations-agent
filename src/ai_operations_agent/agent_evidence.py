"""Request-local, immutable JSON ledger populated exclusively after successful dispatch."""

import json
from collections.abc import Callable

from pydantic import TypeAdapter

from ai_operations_agent.agent_schemas import EvidenceItem, EvidenceReference, EvidenceSource
from ai_operations_agent.tool_schemas import (
    AccessContext,
    QueueIdentity,
    QueueRecommendationsResult,
)


class GroundingError(ValueError):
    pass


def canonical_entries(name: str, snapshot):
    payload = json.dumps(snapshot, allow_nan=False)
    if name == "list_queues":
        queues = TypeAdapter(list[QueueIdentity]).validate_json(payload, strict=True)
        if len({q.id for q in queues}) != len(queues):
            raise GroundingError("duplicate queue")
        return [("queue", f"/{i}", q.id, q.model_dump()) for i, q in enumerate(queues)]
    if name == "get_queue_recommendations":
        result = QueueRecommendationsResult.model_validate_json(payload, strict=True)
        qid = result.assessment.queue_id
        entries = [("assessment", "/assessment", qid, snapshot["assessment"])]
        entries += [("metric", f"/assessment/metrics/{key}", qid, value)
                    for key, value in snapshot["assessment"]["metrics"].items()]
        entries += [("recommendation", f"/recommendations/{i}", qid, value)
                    for i, value in enumerate(snapshot["recommendations"])]
        return entries
    raise GroundingError("unsupported source")


def validate_response_evidence(response):
    """Contract validation in addition to the runtime's private-ledger provenance check."""
    allowed = {}
    assessment_sources = []
    for source in response.sources:
        for kind, pointer, qid, value in canonical_entries(source.tool_name, source.snapshot):
            ref = EvidenceReference(source_id=source.source_id, kind=kind, pointer=pointer)
            allowed[ref] = EvidenceItem(reference=ref, queue_id=qid, value=value)
        if source.tool_name == "get_queue_recommendations":
            assessment_sources.append(source)
    if len(assessment_sources) > 1:
        raise GroundingError("multiple assessments")
    if any(item.reference not in allowed or
           allowed[item.reference].model_dump_json() != item.model_dump_json()
           for item in response.evidence):
        raise GroundingError("evidence does not resolve")
    if response.status != "answered":
        if response.evidence or any(block.kind != "inability" for block in response.explanation):
            raise GroundingError("operational claims after failure")
        return
    if not response.sources or len({s.tool_name for s in response.sources}) != len(response.sources):
        raise GroundingError("invalid answered sources")
    if assessment_sources:
        source = assessment_sources[0]
        required = [ref for ref in allowed if ref.source_id == source.source_id
                    and ref.kind in {"assessment", "recommendation"}]
        if [item.reference for item in response.evidence[:len(required)]] != required:
            raise GroundingError("missing or reordered canonical evidence")
        if any(item.reference.source_id != source.source_id for item in response.evidence):
            raise GroundingError("mixed queue-focus evidence")
        cited = [ref for block in response.explanation for ref in block.references
                 if ref.kind == "recommendation"]
        if cited != required[1:]:
            raise GroundingError("missing or reordered recommendation blocks")
    elif [item.reference for item in response.evidence] != list(allowed):
        raise GroundingError("incomplete discovery")


class EvidenceLedger:
    def __init__(self, run_id: str, access: AccessContext, new_id: Callable[[], str]):
        self.run_id = run_id
        self._access = access
        self._new_id = new_id
        self._sources: dict[str, str] = {}
        self._items: dict[EvidenceReference, str] = {}

    def capture(self, name: str, snapshot: dict | list) -> EvidenceSource:
        """Internal runtime operation, never exposed as an adapter capability."""
        payload = json.dumps(snapshot, allow_nan=False)
        entries = canonical_entries(name, snapshot)
        if any(qid not in self._access.allowed_queue_ids or (
            self._access.selected_queue_id is not None and qid != self._access.selected_queue_id
        ) for _, _, qid, _ in entries):
            raise GroundingError("source outside scope")
        source_id = self._new_id()
        if source_id in self._sources or source_id == self.run_id:
            raise GroundingError("duplicate source ID")
        source = EvidenceSource(source_id=source_id, run_id=self.run_id,
                                tool_name=name, snapshot=json.loads(payload))
        self._sources[source_id] = source.model_dump_json()
        for kind, pointer, qid, value in entries:
            ref = EvidenceReference(source_id=source_id, kind=kind, pointer=pointer)
            self._items[ref] = EvidenceItem(reference=ref, queue_id=qid, value=value).model_dump_json()
        return source

    @property
    def sources(self) -> tuple[EvidenceSource, ...]:
        return tuple(EvidenceSource.model_validate_json(value) for value in self._sources.values())

    def references(self, source_id: str) -> tuple[EvidenceReference, ...]:
        return tuple(ref for ref in self._items if ref.source_id == source_id)

    def resolve(self, reference: EvidenceReference, queue_id: int | None = None) -> EvidenceItem:
        if reference not in self._items:
            raise GroundingError("unsupported reference")
        item = EvidenceItem.model_validate_json(self._items[reference])
        if queue_id is not None and item.queue_id != queue_id:
            raise GroundingError("wrong queue")
        return item
