"""Typed HTTP clients over `wire.ROUTES`. Standard library only; no `ai`.

Transport timeout per request: `wait_s + 10 s` for a long poll (claim, events,
next_launch), else `timeout_s` (default 5 s). Retry rules (the replay rules are in the
`wire.py` header):
- Owner and agent writes: a connection error, a timeout, or `503` is retried with the
  same request ID and body, with backoff 0.1 s to 2 s, until `deadline`
  (`time.monotonic()`), then `Unreachable`. Other non-2xx: `Rejected` at once.
- `claim`: retried with the same request ID (also on 503 while its copy is made), so a
  lost reply gives the same claim; a `stale` reply ends it (the caller makes a new ID).
- `heartbeat`, `launch_heartbeat`, `next_launch`: one try, never retried here (the runner
  starts a new poll after its own backoff).
- `grant_launch`, `report_exit`: retried with the same body until `deadline`.
"""

from __future__ import annotations

from typing import Any

from swarm import wire


class Rejected(Exception):
    """The server refused the request (`wire.ErrorReply`)."""

    def __init__(self, status: int, reply: wire.ErrorReply) -> None:
        super().__init__(f"{status} {reply.error}: {reply.detail}")
        self.status = status
        self.reply = reply

    @property
    def stale(self) -> bool:
        return self.reply.error == "stale"


class Unreachable(Exception):
    """No reply before the deadline."""


def new_request_id() -> str:
    raise NotImplementedError


class HttpClient:
    def __init__(self, base_url: str, token: str | None) -> None:
        raise NotImplementedError

    def call(self, route: str, body: wire.WireModel | None = None, *, deadline: float | None = None,
             retry: bool = True, timeout_s: float = 5.0, **path: Any) -> wire.WireModel | None:
        """Send `wire.ROUTE[route]`. GET: `body` becomes the query string. Returns the
        parsed `route.reply`, or None for 204. `retry=False`: exactly one try."""
        raise NotImplementedError


class OwnerClient:
    """`request_id` arguments default to a new ID; a CLI rerun can pass the same one."""

    def __init__(self, base_url: str, token: str) -> None:
        raise NotImplementedError

    def health(self) -> wire.Health: raise NotImplementedError
    def create_task(self, spec: wire.TaskSpec, request_id: str | None = None) -> wire.TaskCreated: raise NotImplementedError
    def get_task(self, task_id: str) -> wire.TaskView: raise NotImplementedError
    def cancel_task(self, task_id: str, request_id: str | None = None) -> wire.TaskView: raise NotImplementedError
    def history(self, agent_id: str, after_seq: int = -1, limit: int = 200) -> wire.HistoryPage: raise NotImplementedError
    def events(self, task_id: str | None = None, after: int = 0, limit: int = 200, wait_s: float = 0) -> wire.EventsPage: raise NotImplementedError
    def resolve_call(self, agent_id: str, step_seq: int, call_id: str, result: wire.JSON,
                     is_error: bool = False, request_id: str | None = None) -> wire.CallView: raise NotImplementedError
    def processes(self, task_id: str) -> wire.ProcessList: raise NotImplementedError
    def read_log(self, process_id: str, offset: int = 0, max_bytes: int = 65536) -> wire.LogChunk: raise NotImplementedError
    def stop_process(self, process_id: str, request_id: str | None = None) -> wire.ProcessView: raise NotImplementedError

    def wait_for_task(self, task_id: str, timeout_s: float = 60) -> wire.TaskView:
        """Follow events until the task is not `running`; return its view."""
        raise NotImplementedError


class AgentClient:
    """Calls of one worker. Each write makes one request ID and keeps it across retries."""

    def __init__(self, base_url: str, token: str) -> None:
        raise NotImplementedError

    def claim(self, worker_id: str, pid: int, kinds: list[wire.AgentKind], wait_s: float = 20) -> wire.Claim | None: raise NotImplementedError
    def heartbeat(self, agent_id: str, attempt: int, timeout_s: float = 1.0) -> wire.HeartbeatReply: raise NotImplementedError
    def admit_model_call(self, agent_id: str, attempt: int, step_seq: int, estimated_tokens: int, *, deadline: float) -> wire.ModelCallReply: raise NotImplementedError
    def commit_step(self, agent_id: str, step: wire.StepRequest, *, deadline: float) -> wire.StepReply:
        """`step` already carries the attempt and the request ID."""
        raise NotImplementedError
    def start_call(self, agent_id: str, attempt: int, step_seq: int, call_id: str, *, deadline: float) -> wire.CallStartReply: raise NotImplementedError
    def finish_call(self, agent_id: str, attempt: int, step_seq: int, call_id: str, outcome: str, result: wire.JSON,
                    is_error: bool, *, deadline: float) -> wire.CallView: raise NotImplementedError
    def yield_agent(self, agent_id: str, attempt: int, *, deadline: float) -> wire.YieldReply: raise NotImplementedError
    def finish_agent(self, agent_id: str, attempt: int, status: str, result: wire.JSON, *, deadline: float) -> wire.AgentStateReply: raise NotImplementedError


class RunnerClient:
    def __init__(self, base_url: str, token: str) -> None:
        raise NotImplementedError

    def next_launch(self, runner_id: str, wait_s: float = 10) -> wire.LaunchOrder | None: raise NotImplementedError


class LaunchClient:
    """Calls of one shim, authenticated by the single-use launch secret."""

    def __init__(self, base_url: str, launch_id: str, secret: str) -> None:
        raise NotImplementedError

    def grant(self, pid: int, *, deadline: float) -> wire.GrantReply: raise NotImplementedError
    def heartbeat(self, log_bytes: int, truncated_bytes: int, timeout_s: float = 0.5) -> wire.LaunchHeartbeatReply | None:
        """One try; None on any transport error (the shim's loop goes on)."""
        raise NotImplementedError
    def report_exit(self, report: wire.ExitReport, *, deadline: float) -> wire.Ack: raise NotImplementedError
