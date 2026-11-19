"""Tool calls: start (the run grant), finish, owner resolve, fill; server tool dispatch."""

from __future__ import annotations

from collections.abc import Callable

from swarm import wire

from .http import Deps
from .store import Tx

# One-phase server tool: runs inside the start transaction.
ServerTool = Callable[[Tx, Deps, dict, wire.CallView, wire.WireModel], wire.CallStartReply]


def server_tools() -> dict[str, ServerTool]:
    """`memory_set`, `run_child` (tasks); `run_command`, `process_stop` (processes).
    The two-phase tools (`wire.TWO_PHASE_TOOLS`) are dispatched by `start_call` itself."""
    raise NotImplementedError


def start_call(deps: Deps, request: wire.AgentWrite, agent_id: str, step_seq: int, call_id: str) -> wire.CallStartReply:
    """I5. Fence; the call must be in the pending step. By state:
    `done` -> `use`; `unknown` -> `blocked`; `waiting` -> `wait`;
    `running` with this attempt -> `stop` (second start of one grant: the worker exits);
    `running` from an older attempt: `idempotent` -> `run` again (runs+1);
    `planned`: one-phase server tool -> run it in this transaction; local tool ->
    `running`, this attempt, runs+1, `run`.
    Two-phase tool: phase 1 (tx) fence, check access, snapshot what to read or copy;
    phase 2 (no tx) the file work; phase 3 (tx) fence again, the call is still `planned`,
    commit the result and the stored reply. A crash between phases commits nothing."""
    raise NotImplementedError


def finish_call(deps: Deps, request: wire.CallFinish, agent_id: str, step_seq: int, call_id: str) -> wire.CallView:
    """I4. Fence; only the attempt that got `run`. `done` stores the result; `unknown`
    (an unsafe tool raised: no proof that it did nothing) makes the call `unknown`."""
    raise NotImplementedError


def resolve_call(deps: Deps, request: wire.ResolveCall, agent_id: str, step_seq: int, call_id: str) -> wire.CallView:
    """I7. Owner. The call must be `unknown` and the agent not terminal (else `conflict`).
    Call `done` with the owner's result; `leases.settle`."""
    raise NotImplementedError


def fill_call(tx: Tx, agent_id: str, step_seq: int, call_id: str, result: wire.CallResult, *, unknown: bool = False) -> None:
    """A `waiting` call becomes `done` (or `unknown`). If the agent has no lease,
    `settle` it; if it is leased, nothing more (its `yield` sees the call done)."""
    raise NotImplementedError


def view(row: dict) -> wire.CallView:
    raise NotImplementedError
