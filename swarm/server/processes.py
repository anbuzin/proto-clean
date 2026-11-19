"""Background processes: records, launches, grant, shim heartbeat, exit, logs, process tools.

The server owns the record and never starts a process. Transitions: the launch table in
ARCHITECTURE.md section 7. Only `processes.current_launch` can change a process row; a
report for any other launch gets a reply but changes nothing. Files:
`processes/<id>/private/{home,tmp}` (payload HOME and TMPDIR), `processes/<id>/work/`
(own clone for `process_start`), `processes/<id>/log`, `processes/<id>/exit.json`.
"""

from __future__ import annotations

from swarm import wire

from .http import Deps
from .store import Tx

# --- Server tools ----------------------------------------------------------------------


def run_command(tx: Tx, deps: Deps, agent: dict, call: wire.CallView, args: wire.RunCommandArgs) -> wire.CallStartReply:
    """One phase. Policy refused: `use` with an error. Else process `requested`
    (lifetime `command`, cwd = this attempt's copy, argv and env from the decision,
    `timeout_s`); call `waiting` with `process_id`; reply `wait` (a barrier)."""
    raise NotImplementedError


def process_start(deps: Deps, agent_id: str, attempt: int, step_seq: int, call_id: str,
                  args: wire.ProcessStartArgs) -> wire.CallStartReply:
    """Two phases. 1: fence, policy (refused: commit an error result). 2: clone this
    attempt's copy to `processes/<id>/work/` (id from the call key). 3: fence again;
    process `requested` (lifetime from args, cwd = work); call `done` with `{process_id}`."""
    raise NotImplementedError


def process_read(deps: Deps, agent_id: str, attempt: int, step_seq: int, call_id: str,
                 args: wire.ProcessReadArgs) -> wire.CallStartReply:
    """Two phases. 1: fence, same task, snapshot (state, final `log_bytes`). 2: `read_log`.
    3: fence again; call `done` with the `LogChunk` dict."""
    raise NotImplementedError


def process_stop(tx: Tx, deps: Deps, agent: dict, call: wire.CallView, args: wire.ProcessStopArgs) -> wire.CallStartReply:
    """One phase. The starting agent or an ancestor: `desired=stopped` ('agent'); `done`."""
    raise NotImplementedError


# --- Runner and shim routes ------------------------------------------------------------


def next_launch(deps: Deps, request: wire.NextLaunchRequest) -> wire.LaunchOrder | None:
    """I12. Long poll. One transaction: oldest `requested` process with `desired=running`
    -> `launching`, tries+1, new launch `issued` (secret hash, `grant_deadline = mono +
    grant_deadline_s`), `current_launch`. The plaintext secret is only in this reply."""
    raise NotImplementedError


def authenticate_launch(tx: Tx, launch_id: str, secret: str | None) -> dict:
    """Inside the caller's write transaction: secret hash matches, else `unauthorized`."""
    raise NotImplementedError


def grant_launch(deps: Deps, request: wire.GrantRequest, launch_id: str) -> wire.GrantReply:
    """`issued`, current, before deadline, `desired=running`: `granted`, pid, grant hash;
    process `running`, `granted_at`, heartbeat clock = now. Same body for a granted
    launch: `granted` again. Anything else: `granted=False`."""
    raise NotImplementedError


def launch_heartbeat(deps: Deps, request: wire.LaunchHeartbeat, launch_id: str) -> wire.LaunchHeartbeatReply:
    """Granted and current: store the clock, `log_bytes`, `truncated_bytes`. `stop` if
    `desired=stopped`, or a `command` process is past `granted_at + timeout_s` (then
    `desired=stopped` 'timeout' in this transaction). Not current or not granted: `stop`."""
    raise NotImplementedError


def report_exit(deps: Deps, request: wire.ExitReport, launch_id: str) -> wire.Ack:
    """I13. Launch `ended` with `exit_json`, `exit_hash`. Current: process `ended`
    (`exited`, `signaled`, or `not_started` if `started` is False), log facts;
    `on_process_end`. Duplicate same body: Ack. Ended with another body: `conflict`."""
    raise NotImplementedError


def on_process_end(tx: Tx, process_id: str) -> None:
    """Every terminal path calls this. Lifetime `command`: fill the call with a
    `CommandResult`. A cut (I16: outcome `unknown`, or `signaled` with a stop reason in
    `wire.CUT_STOP_REASONS`): `local` -> outcome `interrupted`; `external` -> call
    `unknown`. A `timeout` or `agent` stop is a normal result."""
    raise NotImplementedError


# --- Reaper and recovery ---------------------------------------------------------------


def expire_launches(deps: Deps) -> None:
    """`issued` past deadline -> `revoked`; same transaction: process `requested` again,
    or at `launch_tries` `failed` (`not_started`) and `on_process_end`.
    `granted` with no heartbeat for `shim_silence_s`: if `exit.json` exists, apply it as
    a report; else launch `ended`, process `ended` `unknown` with the current file size
    as final `log_bytes`, `on_process_end`. Never relaunched (I12)."""
    raise NotImplementedError


def enforce_lifetimes(deps: Deps) -> None:
    """`requested` or `launching` with `desired=stopped` -> `ended` `not_started`
    (launch revoked), `on_process_end`."""
    raise NotImplementedError


def recover_after_restart(deps: Deps) -> None:
    """Startup: each `issued` launch -> as a deadline expiry; each `granted` launch:
    heartbeat clock = now (the shim gets `shim_silence_s` to reach the new server)."""
    raise NotImplementedError


# --- Owner routes ----------------------------------------------------------------------


def list_processes(deps: Deps, request: None, task_id: str) -> wire.ProcessList:
    raise NotImplementedError


def read_log(deps: Deps, request: wire.LogQuery, process_id: str) -> wire.LogChunk:
    """Exact bytes from `offset`, base64, no transaction open during the read. `eof`: the
    process ended and `next_offset == log_bytes`. `gap`: the file is missing."""
    raise NotImplementedError


def stop_process(deps: Deps, request: wire.OwnerWrite, process_id: str) -> wire.ProcessView:
    """`desired=stopped` ('owner')."""
    raise NotImplementedError
