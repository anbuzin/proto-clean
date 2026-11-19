"""Tasks and the agent tree: create, view, cancel, history, events, children, memory, ends."""

from __future__ import annotations

from swarm import wire

from .http import Deps
from .store import Tx

# --- Owner routes --------------------------------------------------------------------


def create_task(deps: Deps, request: wire.CreateTask) -> wire.TaskCreated:
    """One transaction: task, root agent `ready`, seed memory, event. `spec.external` only
    with kind `external`; `spec.workspace` must be an existing directory."""
    raise NotImplementedError


def get_task(deps: Deps, request: None, task_id: str) -> wire.TaskView:
    """All agents with attempts, calls, memory; `used_tokens` counts estimates where usage is NULL."""
    raise NotImplementedError


def cancel_task(deps: Deps, request: wire.OwnerWrite, task_id: str) -> wire.TaskView:
    """One transaction: task `cancelled`; each non-terminal agent `failed` ('cancelled'),
    open attempt `cancelled`; each live process `desired=stopped` ('cancelled').
    `unknown` calls stay as they are; no resolve after this (agent terminal)."""
    raise NotImplementedError


def get_history(deps: Deps, request: wire.HistoryQuery, agent_id: str) -> wire.HistoryPage:
    raise NotImplementedError


def list_events(deps: Deps, request: wire.EventsQuery) -> wire.EventsPage:
    raise NotImplementedError


# --- One-phase server tools ------------------------------------------------------------


def run_child(tx: Tx, deps: Deps, agent: dict, call: wire.CallView, args: wire.RunChildArgs) -> wire.CallStartReply:
    """I9. depth < max_depth, children < max_children, tools a subset of the parent's;
    else `use` with an error result. Else: child agent `ready` with `child_spec`,
    seed = (parent, parent's current attempt); call `waiting` with `child_agent_id`;
    reply `wait`. Exactly once (I5)."""
    raise NotImplementedError


def memory_set(tx: Tx, deps: Deps, agent: dict, call: wire.CallView, args: wire.MemorySetArgs) -> wire.CallStartReply:
    """Upsert, version+1; call `done` with `{key, value, version}`; reply `use`."""
    raise NotImplementedError


# --- Ends ------------------------------------------------------------------------------


def on_agent_terminal(tx: Tx, agent_id: str) -> None:
    """I17, in each transaction that ends an agent:
    1. Child: fill the parent's call `done` with `{agent_id, status, result}`
       (is_error when failed); `calls.fill_call` settles a parent with no lease.
    2. Processes with lifetime `agent` of this agent: `desired=stopped` ('agent_ended').
    3. `failed`: each non-terminal descendant fails ('ancestor_failed'), recursively.
    4. Root: task `done` or `failed`; processes with lifetime `task`: stopped ('task_ended')."""
    raise NotImplementedError


def child_spec(parent: wire.TaskSpec, args: wire.RunChildArgs) -> wire.TaskSpec:
    """Same model, kind, system, max_output_tokens, budget (shared by the task); prompt
    and tools from `args`; no memory; no workspace (the seed is the pinned parent copy)."""
    raise NotImplementedError
