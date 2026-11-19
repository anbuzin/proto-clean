"""Kind `loop`: the agent loop on the `ai` SDK, one durable step at a time.

The loop is a function of the committed history, so a new worker continues at the same
point. Memory cache: `claim.memory` (it already has every committed `memory_set`); each
`memory_set` result, fresh or stored (`use`), is applied to the cache. The system
message is built from `spec.system` and the cache before each model call. Each turn:

1. History empty: commit kind `user` = `ai.user_message(spec.prompt)` at seq 0.
2. Last step is a `model` step with calls (the pending step): for each call in order,
   `start_call`:
   - `use`: rebuild `ai.tool_result_part` with the call's ID and name and the stored result.
   - `wait`, `blocked`: pending; after a tool in `wire.BARRIER_TOOLS`, start no later call.
   - `stop`: `os._exit(3)`.
   - `run`: set `CallContext`; `async with ai.ToolRunner() as runner:
     result = await runner.schedule(agent.resolve(call))`, where `call` is the original
     SDK `ToolCallPart` of the committed message and `agent` holds the `ai.AgentTool`s; `result.exception` set and
     safety `unsafe` -> `finish_call(unknown)`; else `finish_call(done)` with
     `result.message.tool_results[0]`'s result and `is_error`.
   Any pending: lease rule (lease.py) around `yield_agent`; `continue` -> step 2 again.
   Else commit kind `tools` with `ai.tool_message(*parts)` and `result_call_ids`.
3. Last step is a `model` step with no calls: `finish_agent(done, text)`.
4. Else: `admit_model_call` (estimate from `models.estimate_tokens`); refused -> return.
   `async with ai.stream(model, [system, *history], tools=[e.tool.tool for e in entries],
   params=ai.InferenceRequestParams(output=ai.OutputParams(max_tokens=spec.max_output_tokens)))`
   and drain it fully (the estimate already reserved `max_output_tokens`); then
   `stream.message` and `stream.usage` (None stays None in `wire.Usage`). Commit kind
   `model` with `planned_calls` (I4); state `failed` -> return.

Not used: `Agent.run` and `ai.durable` - this loop must own the boundary between the
committed reply and the run grant of each tool. The loop never sets `replay`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import ai
from ai.types.messages import Message, ToolCallPart, ToolResultPart
from pydantic import ValidationError

from swarm import wire
from swarm.client.api import new_request_id

from .lease import Lease
from .models import estimate_tokens
from .tools import CallContext, Registry, ToolEntry, _current


@contextmanager
def _may_release(lease: Lease) -> Iterator[None]:
    lease.pause()
    try:
        yield
    finally:
        lease.check()


def _commit(lease: Lease, seq: int, kind: wire.StepKind, message: Message, *,
            calls: list[wire.PlannedCall] | None = None, usage: wire.Usage | None = None) -> wire.StepReply:
    lease.check()
    step = wire.StepRequest(
        attempt=lease.attempt, request_id=new_request_id(), expected_seq=seq,
        kind=kind, message=message.model_dump(mode="json"), calls=calls or [], usage=usage,
        result_call_ids=[part.tool_call_id for part in message.tool_results],
    )
    if kind != "model":
        return lease.client.commit_step(lease.agent_id, step, deadline=lease.deadline)
    with _may_release(lease):
        reply = lease.client.commit_step(lease.agent_id, step, deadline=lease.deadline)
    if reply.state == "leased":
        lease.resume()
    return reply


def _admit(lease: Lease, seq: int, estimate: int) -> bool:
    with _may_release(lease):
        reply = lease.client.admit_model_call(lease.agent_id, lease.attempt, seq, estimate, deadline=lease.deadline)
    if reply.admitted:
        lease.resume()
    return reply.admitted


def _yield(lease: Lease) -> bool:
    with _may_release(lease):
        reply = lease.client.yield_agent(lease.agent_id, lease.attempt, deadline=lease.deadline)
    if reply.action == "continue":
        lease.resume()
        return True
    return False


def _finish(lease: Lease, text: str) -> None:
    with _may_release(lease):
        lease.client.finish_agent(lease.agent_id, lease.attempt, "done", text, deadline=lease.deadline)


async def run_loop_agent(lease: Lease, claim: wire.Claim, model: ai.Model, registry: Registry) -> None:
    """Run one claim to its end: finished, yielded, refused, or stale."""
    history = [Message.model_validate(stored.message) for stored in claim.history]
    memory = dict(claim.memory)
    agent = registry.agent(claim.spec.tools)
    entries = registry.entries(claim.spec.tools)
    seq = claim.next_seq
    if not history:
        user = ai.user_message(claim.spec.prompt)
        seq = _commit(lease, seq, "user", user).next_seq
        history.append(user)

    while True:
        last = history[-1]
        if last.role == "assistant":
            if not last.tool_calls:
                _finish(lease, last.text)
                return
            parts = await run_pending_step(lease, claim, agent, registry, seq - 1, last, memory)
            if parts is None:
                if not _yield(lease):
                    return
                continue
            message = ai.tool_message(*parts)
            seq = _commit(lease, seq, "tools", message).next_seq
        else:
            messages = [_system_message(claim.spec.system, memory), *history]
            estimate = estimate_tokens(messages, [entry.tool for entry in entries], claim.spec.max_output_tokens)
            if not _admit(lease, seq, estimate):
                return
            lease.check()
            message, usage = await _stream_model(model, messages, entries, claim.spec.max_output_tokens)
            calls = planned_calls(message, claim.spec.tools, registry)
            reply = _commit(lease, seq, "model", message, calls=calls, usage=usage)
            if reply.state != "leased":
                return
            seq = reply.next_seq
        history.append(message)


def _system_message(system: str, memory: dict[str, str]) -> Message:
    if memory:
        system += "\n\nAgent memory (JSON):\n" + json.dumps(memory, ensure_ascii=False, sort_keys=True)
    return ai.system_message(system)


async def _stream_model(model: ai.Model, messages: list[Message], entries: list[ToolEntry],
                        max_output_tokens: int) -> tuple[Message, wire.Usage | None]:
    params = ai.InferenceRequestParams(output=ai.OutputParams(max_tokens=max_output_tokens))
    async with ai.stream(model, messages, tools=[entry.tool.tool for entry in entries], params=params) as stream:
        async for _ in stream:
            pass
        usage = stream.usage
        if usage is None:
            return stream.message, None
        counts = usage.model_dump(include={"input_tokens", "output_tokens"}, exclude_unset=True)
        return stream.message, wire.Usage.model_validate(counts)


async def run_pending_step(lease: Lease, claim: wire.Claim, agent: ai.Agent, registry: Registry,
                           step_seq: int, message: ai.types.messages.Message,
                           memory: dict[str, str]) -> list | None:
    """Step 2. The tool result parts, or None if the step is pending."""
    parts: list[ToolResultPart] = []
    for call in message.tool_calls:
        lease.check()
        reply = lease.client.start_call(lease.agent_id, lease.attempt, step_seq, call.tool_call_id,
                                        deadline=lease.deadline)
        if reply.action == "stop":
            os._exit(3)
        if reply.action in ("wait", "blocked"):
            if call.tool_name in wire.BARRIER_TOOLS:
                return None
            continue
        outcome = reply.outcome
        if reply.action == "run":
            entry = registry.get(call.tool_name)
            if entry is None or entry.safety == "server":
                raise RuntimeError(f"Invalid local run grant for {call.tool_name}")
            outcome = await _run_local(lease, claim, agent, entry, step_seq, call)
            if outcome is None:
                return None
        if outcome is None:
            raise RuntimeError(f"No result for {call.tool_call_id}")
        if call.tool_name == "memory_set" and not outcome.is_error:
            memory[outcome.result["key"]] = outcome.result["value"]
        parts.append(ai.tool_result_part(call.tool_call_id, tool_name=call.tool_name,
                                         result=outcome.result, is_error=outcome.is_error))
    return parts if len(parts) == len(message.tool_calls) else None


async def _run_local(lease: Lease, claim: wire.Claim, agent: ai.Agent, entry: ToolEntry,
                     seq: int, call: ToolCallPart) -> wire.CallResult | None:
    lease.check()
    context = CallContext(Path(claim.sandbox_path), lease.agent_id, lease.attempt, seq, call.tool_call_id)
    token = _current.set(context)
    try:
        async with ai.ToolRunner() as runner:
            output = await runner.schedule(agent.resolve(call))
    finally:
        _current.reset(token)
    part = output.message.tool_results[0]
    result = part.model_dump(mode="json")["result"]
    lease.check()
    saved = lease.client.finish_call(
        lease.agent_id, lease.attempt, seq, call.tool_call_id,
        "unknown" if output.exception is not None and entry.safety == "unsafe" else "done",
        result, part.is_error, deadline=lease.deadline,
    )
    return saved.outcome if saved.state == "done" else None


def planned_calls(message: ai.types.messages.Message, spec_tools: list[str], registry: Registry) -> list[wire.PlannedCall]:
    """One `PlannedCall` per tool call, in order. `error` set when the tool is not in the
    spec or the registry, or `tool_args` is not a JSON object; safety from the registry
    (`server` when `error` is set)."""
    plans = []
    for call in message.tool_calls:
        args, parse_error = _parse_args(call.tool_args)
        entry = registry.get(call.tool_name)
        error = parse_error
        if entry is None or call.tool_name not in spec_tools:
            error = f"Tool is not available: {call.tool_name}"
        elif error is None and entry.tool.validator is not None:
            try:
                entry.tool.validator.model_validate(args)
            except ValidationError:
                error = f"Invalid arguments for {call.tool_name}"
        plans.append(wire.PlannedCall(call_id=call.tool_call_id, tool=call.tool_name, args=args,
                                     safety=entry.safety if entry is not None and error is None else "server",
                                     error=error))
    return plans


def _parse_args(raw: str) -> tuple[wire.JSON, str | None]:
    try:
        value = json.loads(raw)
    except ValueError:
        return raw, "Tool arguments are not valid JSON"
    return value, None if isinstance(value, dict) else "Tool arguments must be a JSON object"
