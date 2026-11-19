"""Kind `loop`: the agent loop on the `ai` SDK, one durable step at a time.

The loop is a pure function of the committed history: after a crash, a new worker
reads the same history and continues at the same point. Each turn:

1. History empty: commit `ai.user_message(spec.prompt)` as seq 0.
2. Last message is a model reply with tool calls (the pending step): for each call in
   order, `start_call`:
   - `use`: take the stored result.
   - `wait` (child, command), `blocked` (unknown): mark pending; for a tool in
     `wire.BARRIER_TOOLS`, start no later call of this step.
   - `stop`: `os._exit(3)`.
   - `run`: set `tools.CallContext`, run `ToolRunner().schedule(agent.resolve(call))`,
     then `finish_call` (an exception from an `unsafe` tool gives `unknown`).
   If any call is pending: pause the lease, `yield_agent`; `continue` -> go on;
   `released` -> return. Else commit `ai.tool_message(*results)`.
3. Last message is a model reply with no tool calls: `finish_agent(done, text)`.
4. Else (last is user or tool): system message from `spec.system` and memory;
   `admit_model_call` (refused -> return); `ai.stream(model, [system, *history],
   tools=...)`; commit the reply and its planned calls in one step, with usage (I4).

Not used on purpose: `Agent.run` and `ai.durable` (they start a tool before the
reply is committed). The server stores messages; the loop never sets `replay`.
"""

from __future__ import annotations

import ai

from swarm import wire

from .lease import Lease
from .tools import Registry


async def run_loop_agent(lease: Lease, claim: wire.Claim, model: ai.Model, registry: Registry) -> None:
    """Run one claim to its end: finished, yielded, refused, or stale. The caller enters
    the model factory context and closes it after this returns."""
    raise NotImplementedError


async def run_pending_step(lease: Lease, claim: wire.Claim, agent: ai.Agent, registry: Registry,
                           step_seq: int, message: ai.types.messages.Message) -> list | None:
    """Step 2 above. Returns the tool result parts, or None if the step is pending."""
    raise NotImplementedError


def planned_calls(message: ai.types.messages.Message, registry: Registry) -> list[wire.PlannedCall]:
    """One `PlannedCall` per tool call of a model reply; safety from the registry; args
    parsed from `tool_args` JSON. A tool that is not in the spec gives an error result
    later; it is still recorded."""
    raise NotImplementedError
