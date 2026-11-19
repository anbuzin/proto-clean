"""Process records, server tools and exact-byte log reads.

Launch handshakes and recovery live in ``_launches``. The server never starts a
payload; it only records the order and accepts facts from the granted shim.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from swarm import wire

from . import calls, sandboxes
from .http import Deps
from .policy import Decision
from .store import Tx, fingerprint, json_text, new_id


def _view(row: dict) -> wire.ProcessView:
    fields = {key: row[key] for key in wire.ProcessView.model_fields if key in row}
    return wire.ProcessView(**fields, argv=json.loads(row["argv_json"]))


def _record(tx: Tx, deps: Deps, agent: dict, call: wire.CallView, process_id: str,
            argv: list[str], decision: Decision, cwd: str, lifetime: str, timeout: float | None) -> None:
    log = deps.config.processes_dir / process_id / "log"
    tx.run("""INSERT INTO processes
        (id, task_id, agent_id, call_ref, argv_json, env_json, cwd, effect_class,
         lifetime, timeout_s, desired, state, log_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 'requested', ?, ?)""",
        process_id, agent["task_id"], agent["id"],
        f"{call.agent_id}:{call.step_seq}:{call.call_id}", json_text(argv),
        json_text(decision.env), cwd, decision.effect_class, lifetime, timeout, str(log), tx.now)
    tx.event(agent["task_id"], agent["id"], "process_requested", process_id=process_id)


def _attempt_path(tx: Tx, agent: dict) -> str:
    return tx.require("SELECT sandbox_path FROM attempts WHERE agent_id=? AND attempt=?",
                      agent["id"], agent["attempt"])["sandbox_path"]


def _decision(deps: Deps, process_id: str, argv: list[str]) -> Decision:
    return deps.policy.check(argv, deps.config.processes_dir / process_id / "private")


def _error(tx: Tx, call: wire.CallView, message: str) -> wire.CallStartReply:
    return calls.complete(tx, call, wire.CallResult(result=message, is_error=True))


def run_command(tx: Tx, deps: Deps, agent: dict, call: wire.CallView,
                args: wire.RunCommandArgs) -> wire.CallStartReply:
    """Request an approved command and wait for its process, or complete with an error.

    This one-phase barrier uses the current attempt's sandbox and the policy environment.
    """
    process_id = new_id("proc")
    decision = _decision(deps, process_id, args.argv)
    if not decision.allowed:
        return _error(tx, call, decision.reason)
    _record(tx, deps, agent, call, process_id, args.argv, decision,
            _attempt_path(tx, agent), "command", args.timeout_s)
    return calls.wait(tx, call, process_id=process_id)


def process_start(deps: Deps, request: wire.AgentWrite, agent_id: str, step_seq: int,
                  call_id: str, args: wire.ProcessStartArgs) -> wire.CallStartReply:
    """Clone the attempt and request a background process in two phases.

    Policy refusal or clone failure completes the call with an error. Each try uses a
    provisional process ID; the second fence and replay check choose one winner.
    """
    with deps.store.write() as tx:
        agent, call, reply = calls.start_context(tx, deps, request, agent_id, step_seq, call_id)
        if reply is not None:
            return calls.save_start(tx, deps, request, agent_id, step_seq, call_id, reply)
        process_id = new_id("proc")
        decision = _decision(deps, process_id, args.argv)
        if not decision.allowed:
            return calls.save_start(tx, deps, request, agent_id, step_seq, call_id,
                                    _error(tx, call, decision.reason))
        source = Path(_attempt_path(tx, agent))
    work = deps.config.processes_dir / process_id / "work"
    error = None
    try:
        sandboxes.materialize(source, work)
    except OSError as exc:
        error = str(exc)
    with deps.store.write() as tx:
        agent, call, reply = calls.start_context(tx, deps, request, agent_id, step_seq, call_id)
        if reply is not None:
            return calls.save_start(tx, deps, request, agent_id, step_seq, call_id, reply)
        if error is not None:
            reply = _error(tx, call, error)
        else:
            _record(tx, deps, agent, call, process_id, args.argv, decision, str(work), args.lifetime, None)
            reply = calls.complete(tx, call, wire.CallResult(result={"process_id": process_id}))
        return calls.save_start(tx, deps, request, agent_id, step_seq, call_id, reply)


def _same_task(tx: Tx, agent: dict, process_id: str) -> dict | None:
    return tx.one("SELECT * FROM processes WHERE id=? AND task_id=?", process_id, agent["task_id"])


def process_read(deps: Deps, request: wire.AgentWrite, agent_id: str, step_seq: int,
                 call_id: str, args: wire.ProcessReadArgs) -> wire.CallStartReply:
    """Read a same-task process log outside the transaction and complete after a second fence.

    The first transaction snapshots the process state and final log size. A missing or
    inaccessible process completes the call with an error.
    """
    with deps.store.write() as tx:
        agent, call, reply = calls.start_context(tx, deps, request, agent_id, step_seq, call_id)
        if reply is not None:
            return calls.save_start(tx, deps, request, agent_id, step_seq, call_id, reply)
        row = _same_task(tx, agent, args.process_id)
        if row is None:
            reply = _error(tx, call, "process not found or inaccessible")
            return calls.save_start(tx, deps, request, agent_id, step_seq, call_id, reply)
    chunk = _read_snapshot(row, wire.LogQuery(offset=args.offset, max_bytes=args.max_bytes))
    with deps.store.write() as tx:
        agent, call, reply = calls.start_context(tx, deps, request, agent_id, step_seq, call_id)
        if reply is not None:
            return calls.save_start(tx, deps, request, agent_id, step_seq, call_id, reply)
        if _same_task(tx, agent, args.process_id) is None:
            reply = _error(tx, call, "process not found or inaccessible")
        else:
            reply = calls.complete(tx, call, wire.CallResult(result=chunk.model_dump(mode="json")))
        return calls.save_start(tx, deps, request, agent_id, step_seq, call_id, reply)


def _stop(tx: Tx, row: dict, reason: str) -> None:
    if row["desired"] == "running" and row["state"] not in ("ended", "failed"):
        tx.run("UPDATE processes SET desired='stopped', stop_reason=? WHERE id=?", reason, row["id"])
        tx.event(row["task_id"], row["agent_id"], "process_stop_requested", process_id=row["id"], reason=reason)


def process_stop(tx: Tx, deps: Deps, agent: dict, call: wire.CallView,
                 args: wire.ProcessStopArgs) -> wire.CallStartReply:
    """Stop a process when the caller is its starting agent or an ancestor."""
    row = _same_task(tx, agent, args.process_id)
    if row is None:
        return _error(tx, call, "process not found or inaccessible")
    ancestor = tx.one("""WITH RECURSIVE ancestors(id, parent_id) AS (
        SELECT id, parent_id FROM agents WHERE id=?
        UNION ALL SELECT a.id, a.parent_id FROM agents a JOIN ancestors p ON a.id=p.parent_id)
        SELECT id FROM ancestors WHERE id=?""", row["agent_id"], agent["id"])
    if ancestor is None:
        return _error(tx, call, "only the starting agent or an ancestor may stop this process")
    _stop(tx, row, "agent")
    return calls.complete(tx, call, wire.CallResult(result={"process_id": row["id"]}))


def on_process_end(tx: Tx, process_id: str) -> None:
    """Fill a command's waiting call when its process becomes terminal.

    Unknown or externally stopped commands are cuts: local cuts are interrupted;
    external cuts become unknown. A timeout or agent stop is a normal result.
    The output tail comes only from the stored launch receipt.
    """
    row = tx.require("SELECT * FROM processes WHERE id=?", process_id)
    if row["lifetime"] != "command":
        return
    call = tx.one("SELECT * FROM calls WHERE process_id=? AND state='waiting'", process_id)
    if call is None:
        return
    launch = tx.one("SELECT exit_json FROM launches WHERE id=?", row["current_launch"])
    receipt = json.loads(launch["exit_json"]) if launch and launch["exit_json"] else {}
    cut = row["outcome"] == "unknown" or (row["outcome"] == "signaled" and row["stop_reason"] in wire.CUT_STOP_REASONS)
    outcome = "interrupted" if cut and row["effect_class"] == "local" else row["outcome"]
    result = wire.CommandResult(process_id=process_id, outcome=outcome, exit_code=row["exit_code"],
                                signal=row["signal"], stop_reason=row["stop_reason"], log_bytes=row["log_bytes"],
                                truncated_bytes=row["truncated_bytes"], output_tail=receipt.get("output_tail", ""))
    calls.fill_call(tx, call["agent_id"], call["step_seq"], call["call_id"],
                    wire.CallResult(result=result.model_dump(mode="json")),
                    unknown=cut and row["effect_class"] == "external")


def list_processes(deps: Deps, request: None, task_id: str) -> wire.ProcessList:
    """List a task's processes in creation order."""
    with deps.store.read() as tx:
        tx.require("SELECT id FROM tasks WHERE id=?", task_id)
        return wire.ProcessList(processes=[_view(row) for row in tx.all(
            "SELECT * FROM processes WHERE task_id=? ORDER BY created_at, id", task_id)])


def _read_snapshot(row: dict, request: wire.LogQuery) -> wire.LogChunk:
    offset = request.offset
    terminal = row["state"] in ("ended", "failed")
    granted = row["granted_at"] is not None
    data = b""
    gap = False
    if granted:
        try:
            with open(row["log_path"], "rb") as log:
                log.seek(offset)
                limit = min(request.max_bytes, max(0, row["log_bytes"] - offset)) if terminal else request.max_bytes
                data = log.read(limit)
        except FileNotFoundError:
            gap = True
    end = offset + len(data)
    return wire.LogChunk(data_b64=base64.b64encode(data).decode("ascii"), offset=offset,
                         next_offset=end, eof=terminal and (not granted or end == row["log_bytes"]), gap=gap)


def read_log(deps: Deps, request: wire.LogQuery, process_id: str) -> wire.LogChunk:
    """Read exact log bytes outside a transaction.

    An ungranted process has no bytes; not-started is at EOF. A missing log after
    grant is a gap. An ended process reaches EOF at its final log size.
    """
    with deps.store.read() as tx:
        row = tx.require("SELECT * FROM processes WHERE id=?", process_id)
    return _read_snapshot(row, request)


def stop_process(deps: Deps, request: wire.OwnerWrite, process_id: str) -> wire.ProcessView:
    """Request an owner stop and store the reply for replay."""
    fp = fingerprint("stop_process", {"process_id": process_id}, request.model_dump(mode="json"))
    with deps.store.write() as tx:
        stored = deps.store.stored_reply(tx, "owner", request.request_id, fp)
        if stored is not None:
            return wire.ProcessView.model_validate(stored.reply)
        row = tx.require("SELECT * FROM processes WHERE id=?", process_id)
        _stop(tx, row, "owner")
        reply = _view(tx.require("SELECT * FROM processes WHERE id=?", process_id))
        deps.store.save_reply(tx, "owner", request.request_id, fp, reply.model_dump(mode="json"))
        return reply


# Public route and reaper functions are kept here for the HTTP and app modules.
from ._launches import (authenticate_launch, enforce_lifetimes, expire_launches,
                        grant_launch, launch_heartbeat, next_launch,
                        recover_after_restart, report_exit)
