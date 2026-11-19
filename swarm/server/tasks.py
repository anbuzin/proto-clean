"""Tasks and the agent hierarchy: create, view, cancel, child agents, memory, terminal states."""

from __future__ import annotations

from swarm import wire

from .store import Store, Tx

# --- Owner routes --------------------------------------------------------------------


def create_task(store: Store, auth: str, request: wire.CreateTask) -> wire.TaskCreated:
    """One transaction: `tasks` row, root agent (`ready`, depth 0), seed memory, event
    `task_created`. Validates `spec.kind`, and that `spec.external` is set only for `external`."""
    raise NotImplementedError


def get_task(store: Store, auth: str, request: None, task_id: str) -> wire.TaskView:
    """All agents of the task with attempts, calls, memory; `used_tokens` from `model_calls`."""
    raise NotImplementedError


def cancel_task(store: Store, auth: str, request: wire.OwnerWrite, task_id: str) -> wire.TaskView:
    """One transaction: task `cancelled`; each non-terminal agent `failed` ('cancelled'),
    its open attempt ends `cancelled`; each live process gets `desired=stopped` ('cancelled').
    A worker that still holds a lease gets `stale` on its next write (I2)."""
    raise NotImplementedError


def get_history(store: Store, auth: str, request: wire.HistoryQuery, agent_id: str) -> wire.HistoryPage:
    raise NotImplementedError


def list_events(store: Store, auth: str, request: wire.EventsQuery) -> wire.EventsPage:
    """Events after `after`; if none and `wait_s > 0`, wait (no transaction open) and read again."""
    raise NotImplementedError


# --- Server tools (called by calls.start_call inside its transaction) ---------------


def run_child(tx: Tx, agent: dict, call: wire.CallView, args: wire.RunChildArgs) -> wire.CallStartReply:
    """I9. Check depth < max_depth, the parent's child count < max_children, and
    tools subset of the parent's tools; else `use` with an error result. Else in this tx:
    child agent (`ready`, depth+1, derived spec: parent model, `args.prompt`, tools),
    call `waiting` with `child_agent_id`. Reply `wait`. Exactly once (I5)."""
    raise NotImplementedError


def memory_get(tx: Tx, agent: dict, call: wire.CallView, args: wire.MemoryGetArgs) -> wire.CallStartReply:
    """Call `done` with `{key, value or None}`; reply `use`."""
    raise NotImplementedError


def memory_set(tx: Tx, agent: dict, call: wire.CallView, args: wire.MemorySetArgs) -> wire.CallStartReply:
    """Upsert, version+1; call `done` with `{key, value, version}`; reply `use`."""
    raise NotImplementedError


# --- Terminal states -----------------------------------------------------------------


def on_agent_terminal(tx: Tx, agent_id: str) -> None:
    """Run in each transaction that makes an agent `done` or `failed` (I17):
    1. Child: fill the parent's `run_child` call `done` with
       `{agent_id, status, result}` (is_error when failed); then `leases.settle(parent)`.
    2. Stop processes with lifetime `agent` of this agent (`desired=stopped`, reason 'agent_ended').
    3. Root: task state `done` or `failed`; stop processes with lifetime `task`."""
    raise NotImplementedError


def child_spec(parent: wire.TaskSpec, args: wire.RunChildArgs) -> wire.TaskSpec:
    """The derived spec of a child: same model, kind, system, budget (shared by the task);
    prompt and tools from `args`; memory empty; workspace None (seeded from the parent copy)."""
    raise NotImplementedError
