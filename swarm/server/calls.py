"""Tool calls: start (the run grant), checkpoint, finish, owner resolve; server tool dispatch."""

from __future__ import annotations

from collections.abc import Callable

from swarm import wire

from .store import Store, Tx

ServerTool = Callable[[Tx, dict, wire.CallView, wire.WireModel], wire.CallStartReply]


def server_tools() -> dict[str, ServerTool]:
    """Name -> function for each name in `wire.SERVER_TOOLS` (`tasks.run_child`,
    `tasks.memory_*`, `processes.run_command`, `processes.process_*`)."""
    raise NotImplementedError


def start_call(store: Store, auth: str, request: wire.AgentWrite, agent_id: str,
               step_seq: str, call_id: str) -> wire.CallStartReply:
    """I5. Fence; the call must be in the pending step. By state:
    `done` -> `use` with the result; `unknown` -> `blocked`; `waiting` -> `wait`;
    `running` with this attempt -> `stop` (a second start of one grant; the worker exits);
    `running` from an older attempt and safety `idempotent` -> `run` again with its
    `checkpoint` (runs+1); `planned` -> server tool: run it in this transaction;
    local tool: `running`, attempt, runs+1, `run`.
    `process_read` is the one server tool with file I/O: read the bytes first with no
    transaction open, then commit (`processes.process_read`)."""
    raise NotImplementedError


def checkpoint_call(store: Store, auth: str, request: wire.CallCheckpoint, agent_id: str,
                    step_seq: str, call_id: str) -> wire.Ack:
    """Fence; the call is `running` with this attempt. Store `checkpoint_json` (replace).
    For resume data such as an external CLI session ID, saved before the risky part."""
    raise NotImplementedError


def finish_call(store: Store, auth: str, request: wire.CallFinish, agent_id: str,
                step_seq: str, call_id: str) -> wire.CallView:
    """I4. Fence; only the attempt that got `run`. `done` stores the result; `unknown`
    (an unsafe tool raised: no proof that no effect happened) makes the call `unknown`.
    `mark_progress`."""
    raise NotImplementedError


def resolve_call(store: Store, auth: str, request: wire.ResolveCall, agent_id: str,
                 step_seq: str, call_id: str) -> wire.CallView:
    """I7. Owner only; the call must be `unknown`. Call `done` with the owner's result,
    then `leases.settle(agent)`."""
    raise NotImplementedError


def fill_call(tx: Tx, agent_id: str, step_seq: int, call_id: str, result: wire.CallResult) -> None:
    """Set a `waiting` call `done` and settle its agent if it has no lease (I9).
    Used by child ends and process exits."""
    raise NotImplementedError


def view(row: dict) -> wire.CallView:
    raise NotImplementedError
