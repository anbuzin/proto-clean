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

import http.client
import time
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from swarm import wire


class Rejected(Exception):
    """The server refused the request (`wire.ErrorReply`)."""

    def __init__(self, status: int, reply: wire.ErrorReply) -> None:
        super().__init__(f"{status} {reply.error}: {reply.detail}")
        self.status = status
        self.reply = reply

    @property
    def stale(self) -> bool:
        """Whether the server says the attempt is no longer current."""
        return self.reply.error == "stale"


class Unreachable(Exception):
    """No reply before the deadline."""


def new_request_id() -> str:
    """Make an ID for one logical request, not one transport try."""
    return uuid4().hex


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        """Do not forward bearer tokens to a redirected URL."""
        return None


class HttpClient:
    def __init__(self, base_url: str, token: str | None) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def call(self, route: str, body: wire.WireModel | None = None, *, deadline: float | None = None,
             retry: bool = True, timeout_s: float = 5.0, **path: Any) -> wire.WireModel | None:
        """Send `wire.ROUTE[route]`. GET: `body` becomes the query string. Returns the
        parsed `route.reply`, or None for 204. `retry=False`: exactly one try."""
        endpoint = wire.ROUTE[route]
        url = self._base + endpoint.path.format(**{name: quote(str(value), safe="") for name, value in path.items()})
        headers = {"Accept": "application/json"}
        if endpoint.auth != "none" and self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        data = None
        if body is not None and endpoint.method == "GET":
            url += "?" + urlencode(body.model_dump(mode="json", exclude_none=True))
        elif body is not None:
            data = body.model_dump_json().encode()
            headers["Content-Type"] = "application/json"
        request = Request(url, data, headers, method=endpoint.method)
        wait_s = getattr(body, "wait_s", 0)
        if wait_s:
            timeout_s = wait_s + 10
        retry = retry and endpoint.method == "POST" and route not in {
            "heartbeat", "launch_heartbeat", "next_launch",
        }
        if deadline is None:
            deadline = time.monotonic() + timeout_s + (30 if retry else 0)
        return self._send(request, endpoint, deadline, retry, timeout_s)

    def _send(self, request: Request, route: wire.Route, deadline: float, retry: bool,
              timeout_s: float) -> wire.WireModel | None:
        delay = 0.1
        last_error: Exception | None = None
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                with self._opener.open(request, timeout=min(timeout_s, remaining)) as reply:
                    return None if reply.status == 204 else route.reply.model_validate_json(reply.read())
            except HTTPError as error:
                with error:
                    if error.code != 503 or not retry:
                        try:
                            refusal = wire.ErrorReply.model_validate_json(error.read())
                        except (ValueError, OSError, http.client.HTTPException):
                            refusal = wire.ErrorReply(error="internal", detail=str(error.reason))
                        raise Rejected(error.code, refusal) from error
                last_error = error
            except (OSError, http.client.HTTPException) as error:
                last_error = error
            if not retry:
                break
            time.sleep(max(0, min(delay, deadline - time.monotonic())))
            delay = min(delay * 2, 2)
        raise Unreachable(f"No reply for {route.name} before the deadline") from last_error


class OwnerClient:
    """`request_id` arguments default to a new ID; a CLI rerun can pass the same one."""

    def __init__(self, base_url: str, token: str) -> None:
        self._http = HttpClient(base_url, token)

    def health(self) -> wire.Health:
        """Read the server health without credentials."""
        return cast(wire.Health, self._http.call("health"))

    def create_task(self, spec: wire.TaskSpec, request_id: str | None = None) -> wire.TaskCreated:
        """Submit a task and create its root agent."""
        body = wire.CreateTask(spec=spec, request_id=new_request_id() if request_id is None else request_id)
        return cast(wire.TaskCreated, self._http.call("create_task", body))

    def get_task(self, task_id: str) -> wire.TaskView:
        """Read a task and its agent tree."""
        return cast(wire.TaskView, self._http.call("get_task", task_id=task_id))

    def cancel_task(self, task_id: str, request_id: str | None = None) -> wire.TaskView:
        """Cancel a task and return its new view."""
        body = wire.OwnerWrite(request_id=new_request_id() if request_id is None else request_id)
        return cast(wire.TaskView, self._http.call("cancel_task", body, task_id=task_id))

    def history(self, agent_id: str, after_seq: int = -1, limit: int = 200) -> wire.HistoryPage:
        """Read a page of committed messages."""
        query = wire.HistoryQuery(after_seq=after_seq, limit=limit)
        return cast(wire.HistoryPage, self._http.call("get_history", query, agent_id=agent_id))

    def events(self, task_id: str | None = None, after: int = 0, limit: int = 200, wait_s: float = 0) -> wire.EventsPage:
        """Read or long poll a page of events."""
        query = wire.EventsQuery(task_id=task_id, after=after, limit=limit, wait_s=wait_s)
        return cast(wire.EventsPage, self._http.call("list_events", query))

    def resolve_call(self, agent_id: str, step_seq: int, call_id: str, result: wire.JSON,
                     is_error: bool = False, request_id: str | None = None) -> wire.CallView:
        """Give the result of an unknown tool call."""
        body = wire.ResolveCall(result=result, is_error=is_error,
                                request_id=new_request_id() if request_id is None else request_id)
        return cast(wire.CallView, self._http.call("resolve_call", body, agent_id=agent_id,
                                                   step_seq=step_seq, call_id=call_id))

    def processes(self, task_id: str) -> wire.ProcessList:
        """Read the process records of a task."""
        return cast(wire.ProcessList, self._http.call("list_processes", task_id=task_id))

    def read_log(self, process_id: str, offset: int = 0, max_bytes: int = 65536) -> wire.LogChunk:
        """Read a chunk of a process log at a byte offset."""
        query = wire.LogQuery(offset=offset, max_bytes=max_bytes)
        return cast(wire.LogChunk, self._http.call("read_log", query, process_id=process_id))

    def stop_process(self, process_id: str, request_id: str | None = None) -> wire.ProcessView:
        """Ask the server to stop a process."""
        body = wire.OwnerWrite(request_id=new_request_id() if request_id is None else request_id)
        return cast(wire.ProcessView, self._http.call("stop_process", body, process_id=process_id))

    def wait_for_task(self, task_id: str, timeout_s: float = 60) -> wire.TaskView:
        """Follow events until the task is not `running`; return its view."""
        deadline = time.monotonic() + timeout_s
        after = 0
        while True:
            view = cast(wire.TaskView, self._http.call("get_task", task_id=task_id, deadline=deadline))
            if view.state != "running":
                return view
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Unreachable("Task is still running at the deadline")
            query = wire.EventsQuery(task_id=task_id, after=after, wait_s=min(20, remaining / 2))
            page = cast(wire.EventsPage, self._http.call("list_events", query, deadline=deadline))
            after = page.next_after


class AgentClient:
    """Calls of one worker. Each write makes one request ID and keeps it across retries."""

    def __init__(self, base_url: str, token: str) -> None:
        self._http = HttpClient(base_url, token)

    def claim(self, worker_id: str, pid: int, kinds: list[wire.AgentKind], wait_s: float = 20) -> wire.Claim | None:
        """Claim an agent or return None at the end of an idle poll."""
        body = wire.ClaimRequest(worker_id=worker_id, pid=pid, kinds=kinds,
                                 wait_s=wait_s, request_id=new_request_id())
        return cast(wire.Claim | None, self._http.call("claim", body))

    def heartbeat(self, agent_id: str, attempt: int, timeout_s: float = 1.0) -> wire.HeartbeatReply:
        """Try once to renew a lease."""
        return cast(wire.HeartbeatReply, self._http.call("heartbeat", wire.Heartbeat(attempt=attempt),
                                                        agent_id=agent_id, timeout_s=timeout_s, retry=False))

    def admit_model_call(self, agent_id: str, attempt: int, step_seq: int, estimated_tokens: int, *, deadline: float) -> wire.ModelCallReply:
        """Reserve budget before starting a model call."""
        body = wire.ModelCallRequest(attempt=attempt, request_id=new_request_id(), step_seq=step_seq,
                                     estimated_tokens=estimated_tokens)
        return cast(wire.ModelCallReply, self._http.call("admit_model_call", body, deadline=deadline, agent_id=agent_id))

    def commit_step(self, agent_id: str, step: wire.StepRequest, *, deadline: float) -> wire.StepReply:
        """`step` already carries the attempt and the request ID."""
        return cast(wire.StepReply, self._http.call("commit_step", step, deadline=deadline, agent_id=agent_id))

    def start_call(self, agent_id: str, attempt: int, step_seq: int, call_id: str, *, deadline: float) -> wire.CallStartReply:
        """Ask for permission to run a call or use its stored outcome."""
        body = wire.AgentWrite(attempt=attempt, request_id=new_request_id())
        return cast(wire.CallStartReply, self._http.call("start_call", body, deadline=deadline, agent_id=agent_id,
                                                        step_seq=step_seq, call_id=call_id))

    def finish_call(self, agent_id: str, attempt: int, step_seq: int, call_id: str, outcome: str, result: wire.JSON,
                    is_error: bool, *, deadline: float) -> wire.CallView:
        """Commit a tool result or mark its outcome unknown."""
        body = wire.CallFinish(attempt=attempt, request_id=new_request_id(), outcome=outcome,
                               result=result, is_error=is_error)
        return cast(wire.CallView, self._http.call("finish_call", body, deadline=deadline, agent_id=agent_id,
                                                  step_seq=step_seq, call_id=call_id))

    def yield_agent(self, agent_id: str, attempt: int, *, deadline: float) -> wire.YieldReply:
        """Release the lease if a pending call still prevents progress."""
        body = wire.AgentWrite(attempt=attempt, request_id=new_request_id())
        return cast(wire.YieldReply, self._http.call("yield_agent", body, deadline=deadline, agent_id=agent_id))

    def finish_agent(self, agent_id: str, attempt: int, status: str, result: wire.JSON, *, deadline: float) -> wire.AgentStateReply:
        """Commit the final agent state and result."""
        body = wire.FinishRequest(attempt=attempt, request_id=new_request_id(), status=status, result=result)
        return cast(wire.AgentStateReply, self._http.call("finish_agent", body, deadline=deadline, agent_id=agent_id))


class RunnerClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._http = HttpClient(base_url, token)

    def next_launch(self, runner_id: str, wait_s: float = 10) -> wire.LaunchOrder | None:
        """Poll once for a new launch order."""
        body = wire.NextLaunchRequest(runner_id=runner_id, wait_s=wait_s)
        return cast(wire.LaunchOrder | None, self._http.call("next_launch", body, retry=False))


class LaunchClient:
    """Calls of one shim, authenticated by the single-use launch secret."""

    def __init__(self, base_url: str, launch_id: str, secret: str) -> None:
        self._http = HttpClient(base_url, secret)
        self._launch_id = launch_id

    def grant(self, pid: int, *, deadline: float) -> wire.GrantReply:
        """Request the grant before releasing the payload."""
        return cast(wire.GrantReply, self._http.call("grant_launch", wire.GrantRequest(pid=pid),
                                                    deadline=deadline, launch_id=self._launch_id))

    def heartbeat(self, log_bytes: int, truncated_bytes: int, timeout_s: float = 0.5) -> wire.LaunchHeartbeatReply | None:
        """One try; None on any transport error (the shim's loop goes on)."""
        body = wire.LaunchHeartbeat(log_bytes=log_bytes, truncated_bytes=truncated_bytes)
        try:
            return cast(wire.LaunchHeartbeatReply, self._http.call("launch_heartbeat", body, retry=False,
                        timeout_s=timeout_s, launch_id=self._launch_id))
        except Unreachable:
            return None

    def report_exit(self, report: wire.ExitReport, *, deadline: float) -> wire.Ack:
        """Report the exit, retrying the same receipt."""
        return cast(wire.Ack, self._http.call("report_exit", report, deadline=deadline, launch_id=self._launch_id))
