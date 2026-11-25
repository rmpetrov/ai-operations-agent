"""Private, per-case SQLite and fixed fault substitutions for the standalone evaluator."""

from contextlib import ExitStack, contextmanager
from copy import copy
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ai_operations_agent import triage_agent_runtime as rt
from ai_operations_agent.models import Agent, Base, Queue, Ticket
from ai_operations_agent.triage_eval_dataset import DETAIL, TRIAGE


@contextmanager
def private_fixture(data):
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    try:
        Base.metadata.create_all(engine)
        ids = {}
        with Session(engine) as db:
            missing = []
            for key, profile in data.queues.items():
                p = data.profiles[profile] if profile else None
                queue = Queue(name=f"Synthetic {key}", sla_minutes=p.sla_minutes if p else 30)
                db.add(queue)
                db.flush()
                ids[key] = queue.id
                if p is None:
                    missing.append(queue)
                    continue
                agent = Agent(queue_id=queue.id, name="Fixture agent",
                              status="online" if p.online else "offline")
                db.add(agent)
                db.flush()
                for index, t in enumerate(p.tickets):
                    db.add(Ticket(queue_id=queue.id, assigned_agent_id=agent.id if t.assigned else None,
                                  subject="Fixture ticket", external_id=f"{key}-{index}",
                                  status="open", priority="normal",
                                  created_at=data.evaluated_at - timedelta(minutes=t.age_minutes)))
            for queue in missing:
                db.delete(queue)
            db.commit()
        yield engine, ids
    finally:
        engine.dispose()


@contextmanager
def fixed_fault(condition, ids, observation):
    """No scripting interface. Patches live only in the single-process evaluation worker."""
    def hit():
        observation.injections += 1

    turn_at = {"other": "detail", "hidden": "detail", "duplicate": "detail", "raise": "detail",
               "early-detail": "triage", "extra": "triage", "answer": "triage", "refuse": "triage"}
    with ExitStack() as stack:
        if condition in turn_at:
            next_turn = rt._ScriptedAdapter.next_turn

            def turn(adapter, control):
                if control["stage"] != turn_at[condition]:
                    return next_turn(adapter, control)
                hit()
                if condition == "raise":
                    raise RuntimeError("synthetic adapter failure")
                if condition == "refuse":
                    return {"type": "refusal"}
                if condition == "answer":
                    return {"type": "answer", "kind": "discovery"}
                if condition == "extra":
                    return dict(type="tool_request", call_id="triage", name=TRIAGE,
                                arguments={"extra": True})
                key = {"other": "calm", "hidden": "hidden", "early-detail": "all"}.get(condition)
                target = ids[key] if key else control["target_queue_id"]
                return dict(type="tool_request", call_id="triage" if condition == "duplicate" else "detail",
                            name=DETAIL, arguments={"queue_id": target})

            stack.enter_context(patch.object(rt._ScriptedAdapter, "next_turn", turn))
        elif condition == "bad-link":
            capture = rt.TriageEvidenceLedger.capture

            def substitute(ledger, name, snapshot, **kw):
                if name == DETAIL:
                    hit()
                    kw["parent_triage_source_id"] = "unrelated"
                return capture(ledger, name, snapshot, **kw)
            stack.enter_context(patch.object(rt.TriageEvidenceLedger, "capture", substitute))
        elif condition == "omit-detail":
            build = rt.build_triage_answer

            def incomplete(ledger):
                other = copy(ledger)
                other._sources = ledger._sources[:1]
                result = build(other)
                hit()
                return result
            stack.enter_context(patch.object(rt, "build_triage_answer", incomplete))
        elif condition == "build-fault":
            def unavailable(assessment):
                hit()
                raise RuntimeError("synthetic construction failure")
            stack.enter_context(patch.object(rt, "recommendations_for_assessment", unavailable))
        elif condition == "deadline":
            clock = [1.0]
            capture = rt.TriageEvidenceLedger.capture

            def expire(*args, **kwargs):
                parent = capture(*args, **kwargs)
                hit()
                clock[0] += 60
                return parent
            stack.enter_context(patch.object(rt, "monotonic", lambda: clock[0]))
            stack.enter_context(patch.object(rt.TriageEvidenceLedger, "capture", expire))
        yield
