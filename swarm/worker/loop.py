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
     result = await runner.schedule(agent.resolve(call))`; `result.exception` set and
     safety `unsafe` -> `finish_call(unknown)`; else `finish_call(done)` with
     `result.message.tool_results[0]`'s result and `is_error`.
   Any pending: lease rule (lease.py) around `yield_agent`; `continue` -> step 2 again.
   Else commit kind `tools` with `ai.tool_message(*parts)` and `result_call_ids`.
3. Last step is a `model` step with no calls: `finish_agent(done, text)`.
4. Else: `admit_model_call` (estimate from `models.estimate_tokens`); refused -> return.
   `async with ai.stream(model, [system, *history], tools=..., params=OutputParams(
   max_tokens=min(spec.max_output_tokens, remaining)))` and drain it fully; then
   `stream.message` and `stream.usage` (None stays None in `wire.Usage`). Commit kind
   `model` with `planned_calls` (I4); state `failed` -> return.

Not used: `Agent.run` and `ai.durable` - this loop must own the boundary between the
committed reply and the run grant of each tool. The loop never sets `replay`.
"""

from __future__ import annotations

import ai

from swarm import wire

from .lease import Lease
from .tools import Registry


async def run_loop_agent(lease: Lease, claim: wire.Claim, model: ai.Model, registry: Registry) -> None:
    """Run one claim to its end: finished, yielded, refused, or stale."""
    raise NotImplementedError


async def run_pending_step(lease: Lease, claim: wire.Claim, agent: ai.Agent, registry: Registry,
                           step_seq: int, message: ai.types.messages.Message,
                           memory: dict[str, str]) -> list | None:
    """Step 2. The tool result parts, or None if the step is pending."""
    raise NotImplementedError


def planned_calls(message: ai.types.messages.Message, spec_tools: list[str], registry: Registry) -> list[wire.PlannedCall]:
    """One `PlannedCall` per tool call, in order. `error` set when the tool is not in the
    spec or the registry, or `tool_args` is not a JSON object; safety from the registry
    (`server` when `error` is set)."""
    raise NotImplementedError
