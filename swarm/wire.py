"""The wire contract: every HTTP request and reply, typed, shared by server and clients.

This module is the only code that both sides import. It depends on pydantic only;
it must never import `ai`. The server validates each request body with these models;
the clients parse each reply with them. `ROUTES` is the single list of endpoints:
the server's router and the client library are both built from it.

Messages are SDK `Message` JSON (`ai.types.messages.Message.model_dump(mode="json")`).
The server stores them as opaque dicts and never reads their parts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

JSON = Any

# --- Vocabulary -------------------------------------------------------------------------

AgentKind = Literal["loop", "external"]
AgentState = Literal["ready", "leased", "waiting", "blocked", "done", "failed"]
TERMINAL_AGENT_STATES: frozenset[str] = frozenset({"done", "failed"})
TaskState = Literal["running", "done", "failed", "cancelled"]
Safety = Literal["server", "idempotent", "unsafe"]
CallState = Literal["planned", "running", "waiting", "done", "unknown"]
CallAction = Literal["run", "use", "wait", "blocked", "stop"]
AttemptEnd = Literal["yielded", "finished", "lost", "server_restart", "cancelled"]
EffectClass = Literal["local", "external"]
Lifetime = Literal["call", "agent", "task"]
ProcessState = Literal["requested", "launching", "running", "ended", "failed"]
ProcessOutcome = Literal["not_started", "exited", "signaled", "unknown"]

# Tools that the server executes inside `start_call`. All other tools run in the worker.
SERVER_TOOLS: frozenset[str] = frozenset({
    "memory_get", "memory_set", "run_child",
    "run_command", "process_start", "process_read", "process_stop", "process_list",
})
# A barrier tool stops the step: the worker starts no later call of the same step until
# the barrier call is `done`. `run_child` is not a barrier, so children of one step run in parallel.
BARRIER_TOOLS: frozenset[str] = frozenset({"run_command"})


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Task spec (what the owner submits) ------------------------------------------------

class ExternalSpec(WireModel):
    """Options for kind `external`: one turn of a coding CLI through `ai.external`."""
    cli: Literal["fx"] = "fx"
    # What a cut CLI turn does on the next attempt. `block`: the turn becomes `unknown`
    # and the agent waits for the owner. `resume`: the turn runs again with native resume.
    on_cut: Literal["block", "resume"] = "block"


class TaskSpec(WireModel):
    prompt: str
    model: str = "openai/gpt-5-nano"           # Gateway model ID; the worker's model factory resolves it
    kind: AgentKind = "loop"
    system: str = ""
    tools: list[str] = Field(default_factory=list)   # tool names; local ones must exist in the worker registry
    workspace: str | None = None                # owner directory that seeds the root sandbox; None = empty
    memory: dict[str, str] = Field(default_factory=dict)
    budget_tokens: int = Field(default=200_000, gt=0)   # for the whole task, children included
    max_children: int = Field(default=4, ge=0)
    max_depth: int = Field(default=2, ge=0)
    external: ExternalSpec | None = None


# --- Shared views -----------------------------------------------------------------------

class StoredMessage(WireModel):
    seq: int
    attempt: int
    role: str
    message: dict[str, JSON]


class CallResult(WireModel):
    result: JSON
    is_error: bool = False


class CallView(WireModel):
    agent_id: str
    step_seq: int
    call_id: str
    tool: str
    args: dict[str, JSON]
    safety: Safety
    state: CallState
    attempt: int | None = None                  # attempt that holds or held the `run` grant
    runs: int = 0
    checkpoint: dict[str, JSON] | None = None
    outcome: CallResult | None = None
    child_agent_id: str | None = None
    process_id: str | None = None


class AttemptView(WireModel):
    attempt: int
    worker_id: str
    pid: int
    started_at: float
    ended_at: float | None = None
    end_reason: AttemptEnd | None = None
    sandbox_path: str | None = None


class AgentView(WireModel):
    id: str
    task_id: str
    parent_id: str | None
    kind: AgentKind
    state: AgentState
    attempt: int
    strikes: int
    result: JSON = None
    fail_reason: str | None = None
    memory: dict[str, str] = Field(default_factory=dict)
    attempts: list[AttemptView] = Field(default_factory=list)
    calls: list[CallView] = Field(default_factory=list)


class TaskView(WireModel):
    id: str
    state: TaskState
    root_agent_id: str
    budget_tokens: int
    used_tokens: int
    agents: list[AgentView]


class ProcessView(WireModel):
    id: str
    task_id: str
    agent_id: str
    argv: list[str]
    cwd: str
    effect_class: EffectClass
    lifetime: Lifetime
    state: ProcessState
    desired: Literal["running", "stopped"]
    stop_reason: str | None = None
    outcome: ProcessOutcome | None = None
    exit_code: int | None = None
    signal: int | None = None
    log_bytes: int = 0


class Event(WireModel):
    seq: int
    ts: float
    task_id: str
    agent_id: str | None
    type: str
    data: dict[str, JSON]


class Ack(WireModel):
    ok: bool = True


class ErrorReply(WireModel):
    error: Literal["unauthorized", "not_found", "stale", "conflict", "invalid", "busy", "internal"]
    detail: str = ""


# HTTP status for each error kind. `stale` is the fence: the attempt is not current.
ERROR_STATUS: dict[str, int] = {
    "unauthorized": 401, "not_found": 404, "stale": 409, "conflict": 409,
    "invalid": 422, "busy": 503, "internal": 500,
}


# --- Owner API --------------------------------------------------------------------------

class OwnerWrite(WireModel):
    request_id: str


class CreateTask(OwnerWrite):
    spec: TaskSpec


class TaskCreated(WireModel):
    task_id: str
    agent_id: str


class ResolveCall(OwnerWrite):
    """The owner gives the result of an `unknown` call. The agent can then continue."""
    result: JSON
    is_error: bool = False


class HistoryQuery(WireModel):
    after_seq: int = -1


class HistoryPage(WireModel):
    messages: list[StoredMessage]
    next_seq: int


class EventsQuery(WireModel):
    task_id: str | None = None
    after: int = 0
    wait_s: float = Field(default=0, ge=0, le=30)


class EventsPage(WireModel):
    events: list[Event]
    next_after: int


class ProcessList(WireModel):
    processes: list[ProcessView]


class LogQuery(WireModel):
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=65536, gt=0, le=1 << 20)


class LogChunk(WireModel):
    data_b64: str
    offset: int
    next_offset: int
    eof: bool                 # the process ended and all bytes are read
    gap: bool = False         # the log file is gone; no bytes can come


class Health(WireModel):
    ok: bool
    pid: int
    started_at: float


# --- Agent API (worker token; each write is fenced by agent_id and attempt) ------------

class ClaimRequest(WireModel):
    worker_id: str
    pid: int
    kinds: list[AgentKind] = Field(default_factory=lambda: ["loop"])
    wait_s: float = Field(default=20, ge=0, le=20)
    request_id: str


class Claim(WireModel):
    agent_id: str
    task_id: str
    attempt: int
    lease_ttl_s: float
    spec: TaskSpec               # for a child: the derived child spec
    history: list[StoredMessage]
    next_seq: int
    pending_calls: list[CallView]  # the calls of the last step, if it is a model reply with tool calls
    memory: dict[str, str]
    sandbox_path: str            # a new copy for this attempt; never given to another attempt


class AgentWrite(WireModel):
    attempt: int
    request_id: str


class Heartbeat(WireModel):
    attempt: int                 # no request_id: a heartbeat only moves lease_until


class HeartbeatReply(WireModel):
    lease_until: float


class PlannedCall(WireModel):
    call_id: str
    tool: str
    args: dict[str, JSON]
    safety: Safety


class Usage(WireModel):
    input_tokens: int = 0
    output_tokens: int = 0


class ModelCallRequest(AgentWrite):
    step_seq: int                # the seq that the model reply will get
    estimated_tokens: int = Field(gt=0)


class ModelCallReply(WireModel):
    admitted: bool
    remaining_tokens: int


class StepRequest(AgentWrite):
    expected_seq: int
    message: dict[str, JSON]
    calls: list[PlannedCall] = Field(default_factory=list)
    usage: Usage | None = None   # set on a model reply; fills the admitted model call


class StepReply(WireModel):
    next_seq: int


class CallStartReply(WireModel):
    action: CallAction
    outcome: CallResult | None = None          # set for `use`
    checkpoint: dict[str, JSON] | None = None  # set for `run` of a call that ran before


class CallCheckpoint(AgentWrite):
    data: dict[str, JSON]


class CallFinish(AgentWrite):
    outcome: Literal["done", "unknown"]
    result: JSON
    is_error: bool = False


class YieldReply(WireModel):
    action: Literal["continue", "released"]
    state: AgentState


class FinishRequest(AgentWrite):
    status: Literal["done", "failed"]
    result: JSON = None


class AgentStateReply(WireModel):
    state: AgentState


# --- Runner and shim API ----------------------------------------------------------------

class NextLaunchRequest(WireModel):
    runner_id: str
    wait_s: float = Field(default=10, ge=0, le=20)


class LaunchOrder(WireModel):
    launch_id: str
    process_id: str
    secret: str                  # single use; the runner gives it to the shim on stdin only
    argv: list[str]
    cwd: str
    env: dict[str, str]
    log_path: str
    log_cap_bytes: int
    grant_deadline_s: float      # seconds from now


class GrantRequest(WireModel):
    pid: int                     # the held payload child; its pgid equals its pid


class GrantReply(WireModel):
    granted: bool


class LaunchHeartbeat(WireModel):
    log_bytes: int


class LaunchHeartbeatReply(WireModel):
    stop: bool
    grace_s: float = 5.0         # TERM, then KILL after this many seconds


class ExitReport(WireModel):
    started: bool = True        # False: the shim was denied and killed the held child (`not_started`)
    exit_code: int | None = None
    signal: int | None = None
    log_bytes: int = 0
    truncated_bytes: int = 0


# --- Server tool arguments (the server validates them; the worker mirrors them as stubs) -

class MemoryGetArgs(WireModel):
    key: str


class MemorySetArgs(WireModel):
    key: str
    value: str


class RunChildArgs(WireModel):
    prompt: str
    tools: list[str] | None = None   # a subset of the parent's tools; None = the same tools


class RunCommandArgs(WireModel):
    argv: list[str] = Field(min_length=1)
    timeout_s: float = Field(default=600, gt=0, le=3600)


class ProcessStartArgs(WireModel):
    argv: list[str] = Field(min_length=1)
    lifetime: Literal["agent", "task"] = "agent"


class ProcessReadArgs(WireModel):
    process_id: str
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=65536, gt=0, le=65536)


class ProcessStopArgs(WireModel):
    process_id: str


class ProcessListArgs(WireModel):
    pass


SERVER_TOOL_ARGS: dict[str, type[WireModel]] = {
    "memory_get": MemoryGetArgs, "memory_set": MemorySetArgs, "run_child": RunChildArgs,
    "run_command": RunCommandArgs, "process_start": ProcessStartArgs,
    "process_read": ProcessReadArgs, "process_stop": ProcessStopArgs,
    "process_list": ProcessListArgs,
}
assert set(SERVER_TOOL_ARGS) == SERVER_TOOLS


# --- Routes -----------------------------------------------------------------------------

Auth = Literal["none", "owner", "worker", "runner", "launch"]


@dataclass(frozen=True)
class Route:
    """One endpoint. For GET, `request` is the query model; for POST, the JSON body.
    A reply of None from the handler means `204 No Content` (only `claim`, `next_launch`)."""
    name: str
    method: Literal["GET", "POST"]
    path: str                      # str.format template, e.g. "/v1/agents/{agent_id}/steps"
    auth: Auth
    request: type[WireModel] | None
    reply: type[WireModel]


CALL = "/v1/agents/{agent_id}/calls/{step_seq}/{call_id}"

ROUTES: tuple[Route, ...] = (
    # Owner (control plane)
    Route("health", "GET", "/healthz", "none", None, Health),
    Route("create_task", "POST", "/v1/tasks", "owner", CreateTask, TaskCreated),
    Route("get_task", "GET", "/v1/tasks/{task_id}", "owner", None, TaskView),
    Route("cancel_task", "POST", "/v1/tasks/{task_id}/cancel", "owner", OwnerWrite, TaskView),
    Route("get_history", "GET", "/v1/agents/{agent_id}/history", "owner", HistoryQuery, HistoryPage),
    Route("list_events", "GET", "/v1/events", "owner", EventsQuery, EventsPage),
    Route("resolve_call", "POST", CALL + "/resolve", "owner", ResolveCall, CallView),
    Route("list_processes", "GET", "/v1/tasks/{task_id}/processes", "owner", None, ProcessList),
    Route("read_log", "GET", "/v1/processes/{process_id}/log", "owner", LogQuery, LogChunk),
    Route("stop_process", "POST", "/v1/processes/{process_id}/stop", "owner", OwnerWrite, ProcessView),
    # Agent (worker token)
    Route("claim", "POST", "/v1/claims", "worker", ClaimRequest, Claim),
    Route("heartbeat", "POST", "/v1/agents/{agent_id}/heartbeat", "worker", Heartbeat, HeartbeatReply),
    Route("admit_model_call", "POST", "/v1/agents/{agent_id}/model-calls", "worker", ModelCallRequest, ModelCallReply),
    Route("commit_step", "POST", "/v1/agents/{agent_id}/steps", "worker", StepRequest, StepReply),
    Route("start_call", "POST", CALL + "/start", "worker", AgentWrite, CallStartReply),
    Route("checkpoint_call", "POST", CALL + "/checkpoint", "worker", CallCheckpoint, Ack),
    Route("finish_call", "POST", CALL + "/finish", "worker", CallFinish, CallView),
    Route("yield_agent", "POST", "/v1/agents/{agent_id}/yield", "worker", AgentWrite, YieldReply),
    Route("finish_agent", "POST", "/v1/agents/{agent_id}/finish", "worker", FinishRequest, AgentStateReply),
    # Runner (runner token) and shim (single-use launch secret)
    Route("next_launch", "POST", "/v1/runner/launches", "runner", NextLaunchRequest, LaunchOrder),
    Route("grant_launch", "POST", "/v1/launches/{launch_id}/grant", "launch", GrantRequest, GrantReply),
    Route("launch_heartbeat", "POST", "/v1/launches/{launch_id}/heartbeat", "launch", LaunchHeartbeat, LaunchHeartbeatReply),
    Route("report_exit", "POST", "/v1/launches/{launch_id}/exit", "launch", ExitReport, Ack),
)

ROUTE: dict[str, Route] = {route.name: route for route in ROUTES}
assert len(ROUTE) == len(ROUTES)
