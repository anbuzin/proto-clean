"""Tasks and the agent tree: create, view, cancel, history, events, children, memory, ends."""

from __future__ import annotations

import json
from pathlib import Path

from swarm import wire

from . import calls, leases
from ._replies import Reply
from .errors import ApiError
from .http import Deps
from .store import Tx, json_text, new_id

# --- Owner routes --------------------------------------------------------------------


def create_task(deps: Deps, request: wire.CreateTask) -> wire.TaskCreated:
    """One transaction: task, root agent `ready`, seed memory, event. Kind `external`
    with `spec.external` None: store the spec with `ExternalSpec()` defaults. `external`
    set with kind `loop`: `invalid`. `spec.workspace` must be an existing directory."""
    key = Reply.for_request("create_task", request)
    with deps.store.write() as tx:
        if replay := key.read(tx, deps):
            return replay
        spec = request.spec
        if spec.kind == "loop" and spec.external is not None:
            raise ApiError("invalid", "external options require kind external")
        if spec.kind == "external" and spec.external is None:
            spec = spec.model_copy(update={"external": wire.ExternalSpec()})
        if spec.workspace is not None:
            workspace = Path(spec.workspace).absolute()
            if not workspace.is_dir():
                raise ApiError("invalid", "workspace must be a directory")
            spec = spec.model_copy(update={"workspace": str(workspace)})
        task_id, agent_id = new_id("task"), new_id("ag")
        tx.run("INSERT INTO tasks VALUES(?, ?, 'running', ?, ?)", task_id, agent_id, spec.budget_tokens, tx.now)
        _create_agent(tx, task_id, agent_id, spec)
        for name, value in spec.memory.items():
            tx.run("INSERT INTO memory VALUES(?, ?, ?, 1)", agent_id, name, value)
        return key.save(tx, deps, wire.TaskCreated(task_id=task_id, agent_id=agent_id))


def get_task(deps: Deps, request: None, task_id: str) -> wire.TaskView:
    """All agents with attempts, calls, memory; `used_tokens` counts estimates where usage is NULL."""
    with deps.store.read() as tx:
        return _task_view(tx, task_id)


def cancel_task(deps: Deps, request: wire.OwnerWrite, task_id: str) -> wire.TaskView:
    """One transaction: task `cancelled`; each non-terminal agent `failed` ('cancelled'),
    open attempt `cancelled`; each live process `desired=stopped` ('cancelled').
    `unknown` calls stay as they are; no resolve after this (agent terminal)."""
    key = Reply.for_request("cancel_task", request, task_id=task_id)
    with deps.store.write() as tx:
        if replay := key.read(tx, deps):
            return replay
        task = tx.require("SELECT state FROM tasks WHERE id=?", task_id)
        if task["state"] == "running":
            tx.run("UPDATE tasks SET state='cancelled' WHERE id=?", task_id)
            tx.run("UPDATE processes SET desired='stopped', stop_reason='cancelled' WHERE task_id=? "
                   "AND state NOT IN ('ended', 'failed')", task_id)
            for agent in tx.all("SELECT id FROM agents WHERE task_id=? ORDER BY depth DESC", task_id):
                end_agent(tx, agent["id"], "failed", reason="cancelled")
            tx.event(task_id, None, "task_cancelled")
        return key.save(tx, deps, _task_view(tx, task_id))


def get_history(deps: Deps, request: wire.HistoryQuery, agent_id: str) -> wire.HistoryPage:
    """Read a page of committed messages."""
    with deps.store.read() as tx:
        tx.require("SELECT id FROM agents WHERE id=?", agent_id)
        messages = history(tx, agent_id, request.after_seq, request.limit)
        next_seq = (messages[-1].seq if messages else request.after_seq) + 1
        return wire.HistoryPage(messages=messages, next_seq=next_seq)


def list_events(deps: Deps, request: wire.EventsQuery) -> wire.EventsPage:
    """Read events after the cursor, waiting briefly if none are available."""
    def find() -> wire.EventsPage | None:
        with deps.store.read() as tx:
            rows = tx.all("SELECT * FROM events WHERE seq>? AND (? IS NULL OR task_id=?) ORDER BY seq LIMIT ?",
                          request.after, request.task_id, request.task_id, request.limit)
            if not rows:
                return None
            events = [wire.Event(**{key: value for key, value in row.items() if key != "json"},
                                 data=json.loads(row["json"])) for row in rows]
            return wire.EventsPage(events=events, next_after=events[-1].seq)

    return deps.store.poll(request.wait_s, find) or wire.EventsPage(events=[], next_after=request.after)


# --- One-phase server tools ------------------------------------------------------------


def run_child(tx: Tx, deps: Deps, agent: dict, call: wire.CallView, args: wire.RunChildArgs) -> wire.CallStartReply:
    """I9. depth < max_depth, children < max_children, tools a subset of the parent's;
    else `use` with an error result. Else: child agent `ready` with `child_spec`,
    seed = (parent, parent's current attempt); call `waiting` with `child_agent_id`;
    reply `wait`. Exactly once (I5)."""
    spec = wire.TaskSpec.model_validate_json(agent["spec_json"])
    count = tx.require("SELECT count(*) AS n FROM agents WHERE parent_id=?", agent["id"])["n"]
    if agent["depth"] >= spec.max_depth or count >= spec.max_children:
        return calls.complete(tx, call, wire.CallResult(result="child limit reached", is_error=True))
    if args.tools is not None and not set(args.tools).issubset(spec.tools):
        return calls.complete(tx, call, wire.CallResult(result="tools must be a subset", is_error=True))
    child_id = new_id("ag")
    _create_agent(tx, agent["task_id"], child_id, child_spec(spec, args), parent=agent, call=call)
    return calls.wait(tx, call, child_agent_id=child_id)


def memory_set(tx: Tx, deps: Deps, agent: dict, call: wire.CallView, args: wire.MemorySetArgs) -> wire.CallStartReply:
    """Upsert, version+1; call `done` with `{key, value, version}`; reply `use`."""
    tx.run("INSERT INTO memory VALUES(?, ?, ?, 1) ON CONFLICT(agent_id, key) DO UPDATE SET "
           "value=excluded.value, version=version+1", agent["id"], args.key, args.value)
    row = tx.require("SELECT key,value,version FROM memory WHERE agent_id=? AND key=?", agent["id"], args.key)
    return calls.complete(tx, call, wire.CallResult(result=row))


# --- Ends ------------------------------------------------------------------------------


def on_agent_terminal(tx: Tx, agent_id: str) -> None:
    """I17, in each transaction that ends an agent:
    1. Child: fill the parent's call `done` with `{agent_id, status, result}`
       (is_error when failed); `calls.fill_call` settles a parent with no lease.
    2. Processes with lifetime `agent` of this agent: `desired=stopped` ('agent_ended').
    3. `failed`: each non-terminal descendant fails ('ancestor_failed'), recursively.
    4. Root: task `done` or `failed`; processes with lifetime `task`: stopped ('task_ended')."""
    agent = tx.require("SELECT * FROM agents WHERE id=?", agent_id)
    if agent["parent_id"]:
        step, call_id = agent["parent_call"].split(":", 1)
        result = dict(agent_id=agent_id, status=agent["state"], result=_agent_result(agent))
        calls.fill_call(tx, agent["parent_id"], int(step), call_id,
                        wire.CallResult(result=result, is_error=agent["state"] == "failed"))
    _stop_processes(tx, "agent_id", agent_id, "agent_ended", ("agent", "command"))
    if agent["state"] == "failed":
        for child in tx.all("SELECT id FROM agents WHERE parent_id=?", agent_id):
            end_agent(tx, child["id"], "failed", reason="ancestor_failed")
    if not agent["parent_id"]:
        task_id = agent["task_id"]
        tx.run("UPDATE tasks SET state=? WHERE id=? AND state='running'", agent["state"], task_id)
        _stop_processes(tx, "task_id", task_id, "task_ended", ("task",))
        tx.event(task_id, agent_id, "task_ended")


def child_spec(parent: wire.TaskSpec, args: wire.RunChildArgs) -> wire.TaskSpec:
    """Same model, kind, system, max_output_tokens, budget (shared by the task); prompt
    and tools from `args`; no memory; no workspace (the seed is the pinned parent copy)."""
    raise NotImplementedError

    return parent.model_copy(update={"prompt": args.prompt, "tools": parent.tools if args.tools is None else args.tools,
                                     "workspace": None, "memory": {}})


def end_agent(tx: Tx, agent_id: str, state: wire.AgentState, *, result: wire.JSON = None,
              reason: str | None = None) -> None:
    """End an open agent and apply its terminal effects once (I17)."""
    agent = tx.require("SELECT * FROM agents WHERE id=?", agent_id)
    if agent["state"] in wire.TERMINAL_AGENT_STATES:
        return
    tx.run("UPDATE agents SET state=?, result_json=?, fail_reason=? WHERE id=?", state, json_text(result), reason, agent_id)
    if agent["lease_until"] is not None:
        leases.end_attempt(tx, agent_id, agent["attempt"], "cancelled" if reason == "cancelled" else "finished")
    tx.event(agent["task_id"], agent_id, "agent_ended", state=state, reason=reason)
    on_agent_terminal(tx, agent_id)


def used_tokens(tx: Tx, task_id: str) -> int:
    """Count each admitted call at usage, or its estimate until usage is known."""
    return tx.require("SELECT coalesce(sum(coalesce(used_tokens, estimated_tokens)), 0) AS used "
                      "FROM model_calls WHERE task_id=?", task_id)["used"]


def history(tx: Tx, agent_id: str, after: int = -1, limit: int = -1) -> list[wire.StoredMessage]:
    """Return ordered messages without interpreting their content."""
    rows = tx.all("SELECT seq, attempt, role, json FROM messages WHERE agent_id=? AND seq>? ORDER BY seq LIMIT ?",
                  agent_id, after, limit)
    return [wire.StoredMessage(seq=r["seq"], attempt=r["attempt"], role=r["role"], message=json.loads(r["json"]))
            for r in rows]


def memory(tx: Tx, agent_id: str) -> dict[str, str]:
    """Return the current value of each memory key."""
    return {r["key"]: r["value"] for r in tx.all("SELECT key, value FROM memory WHERE agent_id=?", agent_id)}


def _create_agent(tx: Tx, task_id: str, agent_id: str, spec: wire.TaskSpec, *,
                  parent: dict | None = None, call: wire.CallView | None = None) -> None:
    parent_id = parent["id"] if parent else None
    tx.run("INSERT INTO agents(id, task_id, parent_id, parent_call, seed_agent_id, seed_attempt, depth, "
           "kind, spec_json, state, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?)",
           agent_id, task_id, parent_id, f"{call.step_seq}:{call.call_id}" if call else None, parent_id,
           parent["attempt"] if parent else None, parent["depth"] + 1 if parent else 0,
           spec.kind, spec.model_dump_json(), tx.now)
    tx.event(task_id, agent_id, "agent_created", parent_id=parent_id)


def _agent_result(agent: dict) -> wire.JSON:
    return json.loads(agent["result_json"]) if agent["result_json"] is not None else None


def _task_view(tx: Tx, task_id: str) -> wire.TaskView:
    task = tx.require("SELECT id, root_agent_id, state, budget_tokens FROM tasks WHERE id=?", task_id)
    agents = tx.all("SELECT id, task_id, parent_id, kind, state, attempt, strikes, result_json, fail_reason"
                    " FROM agents WHERE task_id=? ORDER BY depth, created_at, id", task_id)
    views = []
    for agent in agents:
        agent_id = agent["id"]
        result = _agent_result(agent)
        del agent["result_json"]
        attempts = tx.all("SELECT attempt,worker_id,pid,started_at,ended_at,end_reason,sandbox_path "
                          "FROM attempts WHERE agent_id=? ORDER BY attempt", agent_id)
        rows = tx.all("SELECT * FROM calls WHERE agent_id=? ORDER BY step_seq, ordinal", agent_id)
        views.append(wire.AgentView(**agent, result=result, memory=memory(tx, agent_id),
                                    attempts=attempts, calls=[calls.view(row) for row in rows]))
    return wire.TaskView(**task, used_tokens=used_tokens(tx, task_id), agents=views)


def _stop_processes(tx: Tx, key: str, value: str, reason: str, lifetimes: tuple[str, ...]) -> None:
    placeholders = ",".join("?" for _ in lifetimes)
    tx.run(f"UPDATE processes SET desired='stopped', stop_reason=? WHERE {key}=? "
           f"AND lifetime IN ({placeholders}) AND desired='running' AND state NOT IN ('ended', 'failed')",
           reason, value, *lifetimes)
