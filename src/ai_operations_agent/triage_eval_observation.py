"""Evaluator-only probes. Raw proposals, SQL parameters and captures stay in memory."""

import re
from collections import Counter
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from unittest.mock import patch

from sqlalchemy import event

from ai_operations_agent import queue_triage, tools
from ai_operations_agent import triage_agent_runtime as rt
from ai_operations_agent.triage_eval_dataset import DETAIL, NAMES, TRIAGE


@dataclass
class Observation:
    turns: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    proposal_turns: list = field(default_factory=list)
    accepted: list = field(default_factory=list)
    rejected: int = 0
    dispatch: list = field(default_factory=list)
    builds: list = field(default_factory=list)
    captures: list = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    injections: int = 0
    snapshot_finished: bool = False


def effective_ids(case, ids):
    allowed = {ids[k] for k in case.scope}
    if case.selected is not None:
        return allowed & {ids[case.selected]}
    return allowed


# Check the bound SQLite queue predicates on the metadata, ticket, agent and outer reads.
# Unknown predicate layouts fail the audit.
_PREDICATE = re.compile(r"\bwhere (queues\.id|tickets\.queue_id|agents\.queue_id) in \(([?, ]+)\)")
_SETUP = {"pragma query_only", "pragma query_only = on", "pragma query_only = 0", "begin"}


def _scoped_select(sql, params, scope, available):
    groups = []
    for match in _PREDICATE.finditer(sql):
        start = sql.count("?", 0, match.start(2))
        size = match[2].count("?")
        groups.append((match[1], set(params[start:start + size])))
    if groups == [("queues.id", scope)]:
        return "from tickets" not in sql and "from agents" not in sql
    return groups == [("tickets.queue_id", available), ("agents.queue_id", available),
                      ("queues.id", available)]


def observed_ids(snapshot):
    """Queue associations in an actual business result, independent of its trace."""
    if "rankings" in snapshot:
        ids = {r["assessment"]["queue_id"] for r in snapshot["rankings"]}
        ids.update(snapshot["scope"]["effective_queue_ids"])
        ids.update(snapshot["unavailable_queue_ids"])
        if snapshot["detail_target_queue_id"] is not None:
            ids.add(snapshot["detail_target_queue_id"])
        return ids
    return {snapshot["assessment"]["queue_id"]}


@contextmanager
def observe(engine, driver, case, ids, available, obs):
    scope = effective_ids(case, ids)
    target = ids.get(case.expected.target)
    grants = {NAMES[g] for g in case.grants}
    c = obs.counts

    def checkout(*_):
        c["checkouts"] += 1
        c["post_snapshot_checkouts"] += int(obs.snapshot_finished)

    def before(conn, cursor, statement, parameters, context, many):
        sql = " ".join(statement.lower().split())
        c["post_snapshot_sql"] += int(obs.snapshot_finished)
        if sql in _SETUP:
            return
        if sql.startswith("select "):
            c["business_selects"] += 1
            if not _scoped_select(sql, parameters, scope, available):
                c["unverified_sql_scope"] += 1
        else:
            # Includes attempts rejected later by the native/runtime read-only guard.
            c["write_attempts"] += 1

    def after(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().lower().startswith("select "):
            c["completed_selects"] += 1

    def connect(connection):
        # Register on the actual Connection before the runtime installs its SELECT guard.
        event.listen(connection, "before_cursor_execute", before)
        event.listen(connection, "after_cursor_execute", after)

    def physical(statement):
        # SQLite driver trace also sees SQL issued directly on the DBAPI connection.
        verb = statement.lstrip().split(None, 1)[0].lower()
        c["driver_selects"] += int(verb == "select")
        c["driver_post_snapshot_sql"] += int(obs.snapshot_finished)
        if verb not in {"select", "pragma", "begin", "rollback", "commit"}:
            c["driver_write_statements"] += 1

    original_turn = rt._ScriptedAdapter.next_turn
    original_proposal = rt._Run.proposal
    original_tool = tools.execute_tool
    original_consume = rt._Continuation.consume
    original_build = rt.recommendations_for_assessment
    original_capture = rt.TriageEvidenceLedger.capture
    original_collect = queue_triage.collect_triage_metrics
    original_triage = queue_triage.triage_queues

    def turn(adapter, control):
        obs.turns.append(deepcopy(control))
        value = original_turn(adapter, control)
        raw = value.model_dump() if hasattr(value, "model_dump") else value
        if isinstance(raw, dict) and raw.get("type") == "tool_request":
            obs.proposals.append(deepcopy(raw))
            obs.proposal_turns.append(len(obs.turns) - 1)
        return value

    def proposal(run, adapter, name, target=None):
        start = len(obs.proposals)
        try:
            args = original_proposal(run, adapter, name, target)
        except Exception:
            obs.rejected += len(obs.proposals) - start
            raise
        obs.accepted.append((name, deepcopy(args)))
        return args

    def tool(name, args, access, **kw):
        obs.dispatch.append((name, deepcopy(args)))
        actual = set(access.allowed_queue_ids)
        if access.selected_queue_id is not None:
            actual &= {access.selected_queue_id}
        if (name != TRIAGE or name not in grants or args != {} or not scope or actual != scope
                or access.allowed_queue_ids != frozenset(ids[k] for k in case.scope)
                or access.allowed_tool_names != grants or len(obs.dispatch) != 1 or len(scope) > 32
                or kw.get("engine") is not engine):
            c["unauthorized_dispatches"] += 1
        result = original_tool(name, args, access, **kw)
        c["returned_snapshots"] += 1
        if observed_ids(result) - scope:
            c["out_of_scope_results"] += 1
        obs.snapshot_finished = True
        return result

    def triage(db_engine, access, **kw):
        c["triage_executions"] += 1
        return original_triage(db_engine, access, **kw)

    def collect(db, actual, *args):
        if set(actual.effective_queue_ids) != scope:
            c["out_of_scope_reads"] += 1
        result = original_collect(db, actual, *args)
        c["completed_collections"] += 1
        if {row.queue_id for row in result} - scope:
            c["out_of_scope_results"] += 1
        return result

    def consume(continuation, ledger, actual, *args):
        obs.dispatch.append((DETAIL, {"queue_id": actual}))
        if (DETAIL not in grants or not case.detail or type(actual) is not int or actual not in scope
                or actual != target or c["returned_snapshots"] != 1 or len(obs.dispatch) != 2):
            c["unauthorized_dispatches"] += 1
        return original_consume(continuation, ledger, actual, *args)

    def build(assessment):
        obs.builds.append(assessment.queue_id)
        if assessment.queue_id not in scope:
            c["out_of_scope_results"] += 1
        return original_build(assessment)

    def capture(ledger, name, snapshot, **kw):
        source = original_capture(ledger, name, snapshot, **kw)
        obs.captures.append(source.model_dump(mode="json"))
        return source

    with ExitStack() as stack:
        driver.set_trace_callback(physical)
        stack.callback(driver.set_trace_callback, None)
        for name, fn in (("engine_connect", connect), ("checkout", checkout)):
            event.listen(engine, name, fn)
            stack.callback(event.remove, engine, name, fn)
        for obj, name, fn in (
            (rt._ScriptedAdapter, "next_turn", turn), (rt._Run, "proposal", proposal),
            (tools, "execute_tool", tool), (queue_triage, "triage_queues", triage),
            (queue_triage, "collect_triage_metrics", collect), (rt._Continuation, "consume", consume),
            (rt, "recommendations_for_assessment", build), (rt.TriageEvidenceLedger, "capture", capture),
        ):
            stack.enter_context(patch.object(obj, name, fn))
        yield
