"""Steps of the agent loop: model-call admission and budget, commit step, yield, finish.

Each write: stored-reply check (replay rules in `wire.py`), `fence`, validate, change,
event, stored reply - one transaction. A reply that ends the attempt is stored terminal.
"""

from __future__ import annotations

from pydantic import ValidationError

from swarm import wire

from . import calls, leases, tasks
from .errors import ApiError
from .deps import Deps
from .store import Tx, json_text
from .writes import Reply, fence, mark_progress


def admit_model_call(deps: Deps, request: wire.ModelCallRequest, agent_id: str) -> wire.ModelCallReply:
    """I11. `step_seq` must be the next seq and the last step `user` or `tools`
    (else `conflict`). `used` = sum over the task of `used_tokens`, or of
    `estimated_tokens` where usage is NULL. `used + estimated > budget`: agent `failed`
    ('budget'), attempt `finished`, `on_agent_terminal`, `admitted=False` (terminal).
    Else insert `model_calls` (one per attempt and seq) and return the remaining tokens."""
    key = Reply.for_request("admit_model_call", request, agent_id=agent_id)
    with deps.store.write() as tx:
        if replay := key.read(tx, deps):
            return replay
        agent = fence(tx, agent_id, request.attempt)
        last = _last(tx, agent_id)
        if last is None or last["kind"] not in {"user", "tools"} or request.step_seq != last["seq"] + 1:
            raise ApiError("conflict", "model call is not the next step")
        if _admitted(tx, agent_id, request.attempt, request.step_seq):
            raise ApiError("conflict", "model call already admitted")
        task_id = agent["task_id"]
        remaining = _remaining(tx, task_id)
        if request.estimated_tokens > remaining:
            tasks.end_agent(tx, agent_id, "failed", reason="budget")
            return key.save(tx, deps, wire.ModelCallReply(admitted=False, remaining_tokens=remaining), terminal=True)
        tx.run("INSERT INTO model_calls(agent_id, attempt, step_seq, task_id, estimated_tokens, admitted_at) "
               "VALUES(?, ?, ?, ?, ?, ?)", agent_id, request.attempt, request.step_seq, task_id,
               request.estimated_tokens, tx.now)
        tx.event(task_id, agent_id, "model_admitted", step_seq=request.step_seq)
        return key.save(tx, deps, wire.ModelCallReply(admitted=True,
                                                       remaining_tokens=remaining - request.estimated_tokens))


def commit_step(deps: Deps, request: wire.StepRequest, agent_id: str) -> wire.StepReply:
    """I4. `expected_seq` must be the next seq (else `conflict`); kind rules in
    `wire.StepRequest`; the message row stores `kind`.
    `model`: the admitted row must exist (exception: the final reply of kind `external`,
    see `wire.StepRequest`); usage fills `used_tokens` (NULL stays NULL); calls get rows
    with `ordinal` = list position: `planned`, or `done` with an error result if `error`
    is set, the tool is not in `spec.tools`, the args are not a JSON object, or
    server-tool args do not validate. Kind `external`: the only allowed call is one
    `cli_turn` with safety `unsafe`, whatever `spec.tools` says; for kind `loop`,
    `cli_turn` is an unknown tool.
    `tools`: the pending step is the last `model` message; `result_call_ids` must equal
    its call IDs ordered by `ordinal`.
    If the usage puts the task over budget: the message commits, every planned call
    becomes `done` with error 'budget', the agent fails ('budget'), reply `failed`
    (terminal) - so no tool of that reply ever starts. `mark_progress`."""
    key = Reply.for_request("commit_step", request, agent_id=agent_id)
    with deps.store.write() as tx:
        if replay := key.read(tx, deps):
            return replay
        agent = fence(tx, agent_id, request.attempt)
        last = _last(tx, agent_id)
        seq = last["seq"] + 1 if last else 0
        if request.expected_seq != seq:
            raise ApiError("conflict", "step sequence is not current")
        _validate_step(tx, agent, request, last)
        roles = {"user": "user", "model": "assistant", "tools": "tool"}
        tx.run("INSERT INTO messages VALUES(?, ?, ?, ?, ?, ?)", agent_id, seq, request.attempt,
               request.kind, roles[request.kind], json_text(request.message))
        over_budget = request.kind == "model" and _commit_model(tx, agent, request)
        mark_progress(tx, agent_id, request.attempt)
        tx.event(agent["task_id"], agent_id, "step_committed", seq=seq, kind=request.kind)
        if over_budget:
            tasks.end_agent(tx, agent_id, "failed", reason="budget")
        reply = wire.StepReply(next_seq=seq + 1, state="failed" if over_budget else "leased")
        return key.save(tx, deps, reply, terminal=over_budget)


def yield_agent(deps: Deps, request: wire.AgentWrite, agent_id: str) -> wire.YieldReply:
    """I9. All calls of the pending step `done`: `continue`, lease kept. Else
    `end_attempt('yielded')` and `released` with the settled state (terminal)."""
    key = Reply.for_request("yield_agent", request, agent_id=agent_id)
    with deps.store.write() as tx:
        if replay := key.read(tx, deps):
            return replay
        fence(tx, agent_id, request.attempt)
        pending = calls.pending(tx, agent_id)
        if all(call.state == "done" for call in pending):
            return key.save(tx, deps, wire.YieldReply(action="continue", state="leased"))
        if any(call.state == "running" for call in pending):
            raise ApiError("conflict", "cannot yield with a local call running")
        state = leases.end_attempt(tx, agent_id, request.attempt, "yielded")
        return key.save(tx, deps, wire.YieldReply(action="released", state=state), terminal=True)


def finish_agent(deps: Deps, request: wire.FinishRequest, agent_id: str) -> wire.AgentStateReply:
    """`done` needs the last step to be a `model` step with no calls. `failed` is refused
    (`conflict`) while a call is `running` unsafe or `unknown`. Agent terminal, attempt
    `finished`, `tasks.on_agent_terminal`. Stored terminal."""
    key = Reply.for_request("finish_agent", request, agent_id=agent_id)
    with deps.store.write() as tx:
        if replay := key.read(tx, deps):
            return replay
        fence(tx, agent_id, request.attempt)
        pending = calls.pending(tx, agent_id)
        if request.status == "done":
            last = _last(tx, agent_id)
            if last is None or last["kind"] != "model" or pending:
                raise ApiError("conflict", "agent needs a final model step")
        elif any(call.state == "unknown" or (call.state == "running" and call.safety == "unsafe")
                 for call in pending):
            raise ApiError("conflict", "agent has an unresolved unsafe call")
        reason = None if request.status == "done" else "worker"
        tasks.end_agent(tx, agent_id, request.status, result=request.result, reason=reason)
        return key.save(tx, deps, wire.AgentStateReply(state=request.status), terminal=True)


def _last(tx: Tx, agent_id: str) -> dict | None:
    return tx.one("SELECT seq, kind FROM messages WHERE agent_id=? ORDER BY seq DESC LIMIT 1", agent_id)


def _admitted(tx: Tx, agent_id: str, attempt: int, seq: int) -> dict | None:
    return tx.one("SELECT 1 FROM model_calls WHERE agent_id=? AND attempt=? AND step_seq=?", agent_id, attempt, seq)


def _remaining(tx: Tx, task_id: str) -> int:
    task = tx.require("SELECT budget_tokens FROM tasks WHERE id=?", task_id)
    return task["budget_tokens"] - tasks.used_tokens(tx, task_id)


def _validate_step(tx: Tx, agent: dict, request: wire.StepRequest, last: dict | None) -> None:
    if request.kind != "model" and (request.calls or request.usage is not None):
        raise ApiError("invalid", "only model steps carry calls and usage")
    if request.kind != "tools" and request.result_call_ids:
        raise ApiError("invalid", "only tools steps carry call result IDs")
    if request.kind == "user":
        if last is not None:
            raise ApiError("conflict", "user step must be first")
    elif request.kind == "tools":
        pending = calls.pending(tx, agent["id"])
        if (not pending or any(call.state != "done" for call in pending)
                or request.result_call_ids != [call.call_id for call in pending]):
            raise ApiError("conflict", "tools step needs all call results in order")
    elif last is None or last["kind"] not in {"user", "tools"}:
        raise ApiError("conflict", "model step must follow a user or tools step")
    elif agent["kind"] == "external" and last["kind"] == "tools":
        if request.calls or request.usage is not None:
            raise ApiError("invalid", "external final step has no calls or usage")
    else:
        if not _admitted(tx, agent["id"], request.attempt, request.expected_seq):
            raise ApiError("conflict", "model step needs admission")
        if len({call.call_id for call in request.calls}) != len(request.calls):
            raise ApiError("invalid", "tool call IDs must be unique within a step")
        if agent["kind"] == "external" and not (len(request.calls) == 1 and request.calls[0].tool == "cli_turn"
                                                  and request.calls[0].safety == "unsafe"):
            raise ApiError("invalid", "external step needs one unsafe cli_turn call")


def _commit_model(tx: Tx, agent: dict, request: wire.StepRequest) -> bool:
    usage = request.usage
    if usage and usage.input_tokens is not None and usage.output_tokens is not None:
        tx.run("UPDATE model_calls SET used_tokens=? WHERE agent_id=? AND attempt=? AND step_seq=?",
               usage.input_tokens + usage.output_tokens, agent["id"], request.attempt, request.expected_seq)
    over_budget = _remaining(tx, agent["task_id"]) < 0
    spec = wire.TaskSpec.model_validate_json(agent["spec_json"])
    for ordinal, call in enumerate(request.calls):
        error = _call_error(spec, call)
        if error is None and over_budget:
            error = "budget"
        result = wire.CallResult(result=error, is_error=True).model_dump_json() if error is not None else None
        safety = "server" if call.tool in wire.SERVER_TOOLS else call.safety
        tx.run("INSERT INTO calls(agent_id, step_seq, call_id, ordinal, tool, args_json, safety, state, result_json) "
               "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)", agent["id"], request.expected_seq, call.call_id, ordinal,
               call.tool, json_text(call.args), safety, "done" if error is not None else "planned", result)
    return over_budget


def _call_error(spec: wire.TaskSpec, call: wire.PlannedCall) -> str | None:
    if call.error is not None:
        return call.error
    if spec.kind == "loop" and (call.tool == "cli_turn" or call.tool not in spec.tools):
        return "tool not allowed"
    if not isinstance(call.args, dict):
        return "tool arguments must be an object"
    if args := wire.SERVER_TOOL_ARGS.get(call.tool):
        try:
            args.model_validate(call.args)
        except ValidationError:
            return "invalid server tool arguments"
    elif call.safety == "server":
        return "unknown server tool"
    return None
