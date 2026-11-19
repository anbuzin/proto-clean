"""Run one agent from its committed history, with no uncommitted tool calls.

A model reply commits before any of its tools run (I4). On resume, each call
uses its stored result or needs a new run grant. Yield at a pending barrier;
commit the tool message only when all results are ready. Memory augments the
system message; it does not change the committed history.
"""

from __future__ import annotations

import json
from pathlib import Path

import ai
from ai.types.messages import Message, ToolCallPart, ToolResultPart
from pydantic import ValidationError

from swarm import wire

from .lease import Lease
from .models import estimate_tokens
from .steps import admit_model, commit_step, finish_agent, yield_agent
from .tools import CallContext, Registry, ToolEntry, call_context


async def run_loop_agent(lease: Lease, claim: wire.Claim, model: ai.Model, registry: Registry) -> None:
    """Run one claim to its end: finished, yielded, refused, or stale."""
    history = [Message.model_validate(stored.message) for stored in claim.history]
    memory = dict(claim.memory)
    agent = registry.agent(claim.spec.tools)
    entries = registry.entries(claim.spec.tools)
    seq = claim.next_seq
    if not history:
        user = ai.user_message(claim.spec.prompt)
        seq = commit_step(lease, seq, "user", user).next_seq
        history.append(user)

    while True:
        last = history[-1]
        if last.role == "assistant":
            if not last.tool_calls:
                finish_agent(lease, last.text)
                return
            parts = await run_pending_step(lease, claim, agent, registry, seq - 1, last, memory)
            if parts is None:
                if not yield_agent(lease):
                    return
                continue
            message = ai.tool_message(*parts)
            seq = commit_step(lease, seq, "tools", message).next_seq
        else:
            messages = [_system_message(claim.spec.system, memory), *history]
            estimate = estimate_tokens(messages, [entry.tool for entry in entries], claim.spec.max_output_tokens)
            if not admit_model(lease, seq, estimate):
                return
            lease.check()
            message, usage = await _stream_model(model, messages, entries, claim.spec.max_output_tokens)
            calls = planned_calls(message, claim.spec.tools, registry)
            reply = commit_step(lease, seq, "model", message, calls=calls, usage=usage)
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
            lease.stop("tool call refused a second run")
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
    with call_context(context):
        async with ai.ToolRunner() as runner:
            output = await runner.schedule(agent.resolve(call))
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
