"""Tool calls: start (the run grant), finish, owner resolve, fill; server tool dispatch."""

from __future__ import annotations

import json
from collections.abc import Callable

from swarm import wire

from . import leases, processes, tasks
from .deps import Deps
from .errors import ApiError
from .store import Tx
from .writes import Reply, fence, mark_progress

# One-phase server tool: runs inside the start transaction.
ServerTool = Callable[[Tx, Deps, dict, wire.CallView, wire.WireModel], wire.CallStartReply]


def server_tools() -> dict[str, ServerTool]:
    """`memory_set`, `run_child` (tasks); `run_command`, `process_stop` (processes).
    The two-phase tools (`wire.TWO_PHASE_TOOLS`) are dispatched by `start_call` itself."""
    return {"memory_set": tasks.memory_set, "run_child": tasks.run_child,
            "run_command": processes.run_command, "process_stop": processes.process_stop}


def start_call(deps: Deps, request: wire.AgentWrite, agent_id: str, step_seq: int, call_id: str) -> wire.CallStartReply:
    """Start one tool call or return its existing decision (I5).

    Only an idempotent local call can run again after an attempt ends. A server
    tool commits with its reply unless it needs file I/O; those tools check the
    fence before and after their file work.
    """
    # Only choose a handler here; each handler checks replay and the fence in its write transaction.
    with deps.store.read() as tx:
        row = tx.one("SELECT tool FROM calls WHERE agent_id=? AND step_seq=? AND call_id=?",
                     agent_id, step_seq, call_id)
    if row and row["tool"] in wire.TWO_PHASE_TOOLS:
        handler = processes.process_start if row["tool"] == "process_start" else processes.process_read
        return handler(deps, request, agent_id, step_seq, call_id)
    with deps.store.write() as tx:
        agent, call, reply = start_context(tx, deps, request, agent_id, step_seq, call_id)
        if reply is not None:
            return save_start(tx, deps, request, agent_id, step_seq, call_id, reply)
        if call.safety == "server":
            args = wire.SERVER_TOOL_ARGS[call.tool].model_validate(call.args)
            reply = server_tools()[call.tool](tx, deps, agent, call, args)
        else:
            tx.run("UPDATE calls SET state='running', attempt=?, runs=runs+1 "
                   "WHERE agent_id=? AND step_seq=? AND call_id=?", request.attempt, agent_id, step_seq, call_id)
            tx.event(agent["task_id"], agent_id, "call_started", step_seq=step_seq, call_id=call_id)
            reply = wire.CallStartReply(action="run")
        return save_start(tx, deps, request, agent_id, step_seq, call_id, reply)


def finish_call(deps: Deps, request: wire.CallFinish, agent_id: str, step_seq: int, call_id: str) -> wire.CallView:
    """I4. Fence; only the attempt that got `run`. `done` stores the result; `unknown`
    (an unsafe tool raised: no proof that it did nothing) makes the call `unknown`."""
    key = Reply.for_request("finish_call", request, agent_id=agent_id, step_seq=step_seq, call_id=call_id)
    with deps.store.write() as tx:
        if replay := key.read(tx, deps):
            return replay
        agent = fence(tx, agent_id, request.attempt)
        call = _call(tx, agent_id, step_seq, call_id)
        if call.state != "running" or call.attempt != request.attempt:
            raise ApiError("conflict", "attempt has no run grant")
        if request.outcome == "unknown":
            if call.safety != "unsafe":
                raise ApiError("conflict", "only unsafe calls can become unknown")
            _set_result(tx, call, None, unknown=True, task_id=agent["task_id"])
        else:
            complete(tx, call, wire.CallResult(result=request.result, is_error=request.is_error))
        return key.save(tx, deps, _call(tx, agent_id, step_seq, call_id))


def resolve_call(deps: Deps, request: wire.ResolveCall, agent_id: str, step_seq: int, call_id: str) -> wire.CallView:
    """I7. Owner. The call must be `unknown` and the agent not terminal (else `conflict`).
    Call `done` with the owner's result; `leases.settle`."""
    key = Reply.for_request("resolve_call", request, agent_id=agent_id, step_seq=step_seq, call_id=call_id)
    with deps.store.write() as tx:
        if replay := key.read(tx, deps):
            return replay
        agent = tx.require("SELECT * FROM agents WHERE id=?", agent_id)
        call = _call(tx, agent_id, step_seq, call_id)
        if agent["state"] in wire.TERMINAL_AGENT_STATES or call.state != "unknown":
            raise ApiError("conflict", "call is not unresolved on an open agent")
        outcome = wire.CallResult(result=request.result, is_error=request.is_error)
        _set_result(tx, call, outcome, task_id=agent["task_id"])
        leases.settle(tx, agent_id)
        return key.save(tx, deps, _call(tx, agent_id, step_seq, call_id))


def fill_call(tx: Tx, agent_id: str, step_seq: int, call_id: str, result: wire.CallResult,
              *, unknown: bool = False) -> None:
    """A `waiting` call becomes `done` (or `unknown`). If the agent has no lease,
    `settle` it; if it is leased, nothing more (its `yield` sees the call done)."""
    call = _call(tx, agent_id, step_seq, call_id)
    if call.state == "waiting":
        agent = tx.require("SELECT * FROM agents WHERE id=?", agent_id)
        _set_result(tx, call, None if unknown else result, unknown=unknown, task_id=agent["task_id"])
        if not unknown and agent["lease_until"] is not None:
            mark_progress(tx, agent_id, agent["attempt"])
        leases.settle(tx, agent_id)


def view(row: dict) -> wire.CallView:
    """Convert a stored call row to its public view."""
    outcome = row["result_json"]
    fields = {key: row[key] for key in wire.CallView.model_fields if key in row}
    return wire.CallView(**fields, args=json.loads(row["args_json"]), outcome=json.loads(outcome) if outcome else None)


def pending(tx: Tx, agent_id: str) -> list[wire.CallView]:
    """Return calls of the last step in message order."""
    return [view(row) for row in tx.all("SELECT * FROM calls WHERE agent_id=? AND step_seq=("
                                       "SELECT max(seq) FROM messages WHERE agent_id=?) ORDER BY ordinal",
                                       agent_id, agent_id)]


def start_context(tx: Tx, deps: Deps, request: wire.AgentWrite, agent_id: str, step_seq: int, call_id: str
                  ) -> tuple[dict | None, wire.CallView | None, wire.CallStartReply | None]:
    """Check replay, fence, pending step and call order for both tool phases."""
    key = Reply.for_request("start_call", request, agent_id=agent_id, step_seq=step_seq, call_id=call_id)
    if replay := key.read(tx, deps):
        return None, None, replay
    agent = fence(tx, agent_id, request.attempt)
    for call in pending(tx, agent_id):
        if (call.step_seq, call.call_id) == (step_seq, call_id):
            return agent, call, _start_action(call, request.attempt)
        if call.state in {"planned", "running"} or (call.tool in wire.BARRIER_TOOLS and call.state != "done"):
            raise ApiError("conflict", "an earlier tool call must run first")
    raise ApiError("conflict", "call is not in the pending step")


def save_start(tx: Tx, deps: Deps, request: wire.AgentWrite, agent_id: str, step_seq: int, call_id: str,
               reply: wire.CallStartReply) -> wire.CallStartReply:
    """Save a start reply after its change is committed in this transaction."""
    key = Reply.for_request("start_call", request, agent_id=agent_id, step_seq=step_seq, call_id=call_id)
    return key.save(tx, deps, reply)


def complete(tx: Tx, call: wire.CallView, result: wire.CallResult) -> wire.CallStartReply:
    """Store a completed tool result and mark the active attempt's progress."""
    agent = tx.require("SELECT task_id, attempt FROM agents WHERE id=?", call.agent_id)
    _set_result(tx, call, result, task_id=agent["task_id"])
    mark_progress(tx, call.agent_id, agent["attempt"])
    return wire.CallStartReply(action="use", outcome=result)


def wait(tx: Tx, call: wire.CallView, *, child_agent_id: str | None = None,
         process_id: str | None = None) -> wire.CallStartReply:
    """Link the call to its child or command until that record ends."""
    tx.run("UPDATE calls SET state='waiting', child_agent_id=?, process_id=? "
           "WHERE agent_id=? AND step_seq=? AND call_id=?", child_agent_id, process_id,
           call.agent_id, call.step_seq, call.call_id)
    task = tx.require("SELECT task_id FROM agents WHERE id=?", call.agent_id)
    tx.event(task["task_id"], call.agent_id, "call_waiting", step_seq=call.step_seq, call_id=call.call_id)
    return wire.CallStartReply(action="wait")


def _call(tx: Tx, agent_id: str, step_seq: int, call_id: str) -> wire.CallView:
    return view(tx.require("SELECT * FROM calls WHERE agent_id=? AND step_seq=? AND call_id=?",
                           agent_id, step_seq, call_id))


def _start_action(call: wire.CallView, attempt: int) -> wire.CallStartReply | None:
    if call.state == "done":
        return wire.CallStartReply(action="use", outcome=call.outcome)
    if call.state == "unknown":
        return wire.CallStartReply(action="blocked")
    if call.state == "waiting":
        return wire.CallStartReply(action="wait")
    if call.state == "running" and (call.attempt == attempt or call.safety != "idempotent"):
        return wire.CallStartReply(action="stop")
    return None


def _set_result(tx: Tx, call: wire.CallView, result: wire.CallResult | None, *, task_id: str,
                unknown: bool = False) -> None:
    state = "unknown" if unknown else "done"
    tx.run("UPDATE calls SET state=?, result_json=? WHERE agent_id=? AND step_seq=? AND call_id=?",
           state, result.model_dump_json() if result is not None else None, call.agent_id, call.step_seq, call.call_id)
    tx.event(task_id, call.agent_id, f"call_{state}", step_seq=call.step_seq, call_id=call.call_id)
