"""Background processes: records, launches, grant, shim heartbeat, exit, logs, process tools.

The server owns the record and the rules; it never starts a process itself. The runner
takes a launch order, starts a shim, and the shim talks to the server directly.
State of a process: `requested` -> `launching` (launch `issued`) -> `running` (launch
`granted`) -> `ended`; or `failed` (`not_started`) after `launch_tries` revoked tries.
Log file: `state_dir/processes/<process_id>/<launch_id>.log`, written only by the shim.
"""

from __future__ import annotations

from swarm import wire

from .store import Store, Tx

# --- Server tools (called by calls.start_call inside its transaction) ---------------


def run_command(tx: Tx, agent: dict, call: wire.CallView, args: wire.RunCommandArgs) -> wire.CallStartReply:
    """Policy check (`Decision`); refused -> `use` with an error result. Else a process
    (lifetime `call`, cwd = this attempt's sandbox copy, `timeout_s`), call `waiting`
    with `process_id`. Reply `wait`. The exit fills the call (`on_process_end`)."""
    raise NotImplementedError


def process_start(tx: Tx, agent: dict, call: wire.CallView, args: wire.ProcessStartArgs) -> wire.CallStartReply:
    """Policy check; a process with lifetime `agent` or `task`; call `done` with
    `{process_id}`. Reply `use`."""
    raise NotImplementedError


def process_stop(tx: Tx, agent: dict, call: wire.CallView, args: wire.ProcessStopArgs) -> wire.CallStartReply:
    """Only the agent that started it, or an ancestor. `desired=stopped` ('agent').
    Call `done` with `{requested: true}`."""
    raise NotImplementedError


def process_list(tx: Tx, agent: dict, call: wire.CallView, args: wire.ProcessListArgs) -> wire.CallStartReply:
    """Processes of the same task; call `done` with a list of `ProcessView` dicts."""
    raise NotImplementedError


def process_read(store: Store, agent_id: str, attempt: int, step_seq: int, call_id: str,
                 args: wire.ProcessReadArgs) -> wire.CallStartReply:
    """Same task only. Read bytes with no transaction open (`read_log`), then commit the
    call `done` with the `LogChunk` dict. Reply `use`."""
    raise NotImplementedError


# --- Runner and shim routes ------------------------------------------------------------


def next_launch(store: Store, auth: str, request: wire.NextLaunchRequest) -> wire.LaunchOrder | None:
    """I12. Long poll. One transaction: oldest `requested` process with `desired=running`
    -> `launching`; tries+1; launch `issued` with a new random secret (store only its
    sha256) and `grant_deadline = now + grant_deadline_s`. The secret is in this reply
    only; a lost reply means the launch expires and the next try gets a new launch."""
    raise NotImplementedError


def authenticate_launch(tx: Tx, launch_id: str, secret: str) -> dict:
    """Check the secret hash; `ApiError('unauthorized')` if wrong. Returns the launch row."""
    raise NotImplementedError


def grant_launch(store: Store, auth: str, request: wire.GrantRequest, launch_id: str) -> wire.GrantReply:
    """I12. Granted only if the launch is `issued`, before its deadline, and the process
    `desired=running`: launch `granted` with `pid`, process `running`. A repeat with the
    same pid of a granted launch gets `granted` again. Else `granted=False`."""
    raise NotImplementedError


def launch_heartbeat(store: Store, auth: str, request: wire.LaunchHeartbeat, launch_id: str) -> wire.LaunchHeartbeatReply:
    """Store `heartbeat_at` and `log_bytes`. `stop=True` if `desired=stopped` or the
    `timeout_s` of a `call` process has passed (then reason 'timeout')."""
    raise NotImplementedError


def report_exit(store: Store, auth: str, request: wire.ExitReport, launch_id: str) -> wire.Ack:
    """I13. One transaction: launch `ended`; process `ended`, outcome `exited` or
    `signaled`; `on_process_end`. A duplicate report gets `Ack`."""
    raise NotImplementedError


def on_process_end(tx: Tx, process_id: str) -> None:
    """For lifetime `call`: fill the `run_command` call (`calls.fill_call`) with
    `{outcome, exit_code, signal, stop_reason, log_bytes}`. A cut (`unknown`, or a stop
    that was not asked by the agent) by class (I16): `local` -> result
    `{outcome: 'interrupted'}`; `external` -> call `unknown`, agent `blocked`."""
    raise NotImplementedError


# --- Reaper and recovery ---------------------------------------------------------------


def expire_launches(store: Store) -> None:
    """`issued` past deadline -> `revoked`; process back to `requested`, or `failed`
    (`not_started`) at `launch_tries`. `granted` with no shim heartbeat for
    `shim_silence_s` -> launch `ended`, process `ended` with outcome `unknown` (I13)."""
    raise NotImplementedError


def enforce_lifetimes(store: Store) -> None:
    """A process whose owner (agent, task) is terminal gets `desired=stopped`. A
    `requested` or `launching` process with `desired=stopped` -> `ended` `not_started`."""
    raise NotImplementedError


def recover_after_restart(store: Store) -> None:
    """Startup: revoke each `issued` launch (its payload never ran)."""
    raise NotImplementedError


# --- Owner routes ----------------------------------------------------------------------


def list_processes(store: Store, auth: str, request: None, task_id: str) -> wire.ProcessList:
    raise NotImplementedError


def read_log(store: Store, auth: str, request: wire.LogQuery, process_id: str) -> wire.LogChunk:
    """Exact bytes from `offset` (base64). `eof` only when the process ended and the
    offset is at the final size; `gap` when the file is missing. No transaction is open
    during the file read."""
    raise NotImplementedError


def stop_process(store: Store, auth: str, request: wire.OwnerWrite, process_id: str) -> wire.ProcessView:
    """`desired=stopped` ('owner')."""
    raise NotImplementedError
