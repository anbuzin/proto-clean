"""Kind `external`: one turn of a coding CLI (Fx) through `ai.external`. Minimal form.

The turn is one `unsafe` tool call, `cli_turn` (call ID `turn`), in the normal step
protocol. The server accepts `cli_turn` only for kind `external`, as the only call.
1. History empty: commit kind `user` (seq 0).
2. `admit_model_call` with `spec.external.turn_tokens`; commit kind `model`: a synthetic
   `ai.assistant_message` with one tool call `cli_turn` (args `{}`).
3. `start_call` -> `run`: open one Fx session in the sandbox;
   send the prompt and read its events through the first `StreamEnd`;
   `finish_call(done)` with the final text. A start refusal is a tool error; a cut turn becomes `unknown`.
   `use` (a turn that finished before a crash): take the stored text.
4. Commit kind `tools`; then the final `model` step with the text, with no admission
   (`wire.StepRequest`); `finish_agent(done)`.
Each claim resumes from the last committed step: it does only the steps above that are
not in `claim.history` yet (a `use` of a finished `cli_turn` skips the CLI).

A worker loss during step 3 leaves the call `running` -> `unknown`: the agent is
`blocked` until the owner resolves it (I6, I7). No resume, no native store. The CLI
inherits `os.environ`, from which the worker removed its token and the model key; it
uses its own Fx login. An Fx process that outlives the worker: "Limits".
"""

from __future__ import annotations

from contextlib import aclosing

import ai
from ai.external import ExecutableMissingError, ExternalHarnessError, Fx
from ai.types.messages import Message, ToolCallPart

from swarm import wire

from .lease import Lease
from .steps import admit_model, commit_step, finish_agent, yield_agent

CLI_TURN = "cli_turn"


async def run_external_agent(lease: Lease, claim: wire.Claim) -> None:
    """Perform or resume one durable Fx turn."""
    history = [Message.model_validate(stored.message) for stored in claim.history]
    seq = claim.next_seq
    if not history:
        message = ai.user_message(claim.spec.prompt)
        seq = commit_step(lease, seq, "user", message).next_seq
        history.append(message)
    if history[-1].role == "user":
        spec = claim.spec.external or wire.ExternalSpec()
        if not admit_model(lease, seq, spec.turn_tokens):
            return
        message = ai.assistant_message(ToolCallPart(tool_name=CLI_TURN, tool_call_id="turn", tool_args="{}"))
        plan = wire.PlannedCall(call_id="turn", tool=CLI_TURN, args={}, safety="unsafe")
        reply = commit_step(lease, seq, "model", message, calls=[plan])
        if reply.state != "leased":
            return
        seq = reply.next_seq
        history.append(message)
    if history[-1].tool_calls:
        while (outcome := await _turn_result(lease, claim, seq - 1)) is None:
            if not yield_agent(lease):
                return
        message = ai.tool_message(tool_call_id="turn", tool_name=CLI_TURN,
                                  result=outcome.result, is_error=outcome.is_error)
        seq = commit_step(lease, seq, "tools", message).next_seq
        history.append(message)
    if history[-1].role == "tool":
        value = history[-1].tool_results[0].result
        message = ai.assistant_message(value if isinstance(value, str) else str(value))
        if commit_step(lease, seq, "model", message).state != "leased":
            return
        history.append(message)
    finish_agent(lease, history[-1].text)


async def _turn_result(lease: Lease, claim: wire.Claim, seq: int) -> wire.CallResult | None:
    lease.check()
    reply = lease.client.start_call(lease.agent_id, lease.attempt, seq, "turn", deadline=lease.deadline)
    if reply.action == "stop":
        lease.stop("Fx tool call refused a second run")
    if reply.action == "use":
        return reply.outcome
    if reply.action != "run":
        return None
    lease.check()
    try:
        text, is_error, outcome = await _run_fx(claim), False, "done"
    except Exception as error:
        # A start failure proves no turn began. Later errors do not prove that (I6).
        not_started = isinstance(error, ExecutableMissingError) or (
            isinstance(error, ExternalHarnessError) and isinstance(error.__cause__, OSError)
            and str(error).startswith("cannot start ")
        )
        text, is_error, outcome = str(error), True, "done" if not_started else "unknown"
    lease.check()
    saved = lease.client.finish_call(lease.agent_id, lease.attempt, seq, "turn", outcome, text, is_error,
                                     deadline=lease.deadline)
    return saved.outcome


async def _run_fx(claim: wire.Claim) -> str:
    async with Fx(cwd=claim.sandbox_path, instructions=claim.spec.system or None) as fx:
        await fx.session.send(ai.user_message(claim.spec.prompt))
        async with aclosing(fx.session.events()) as events:
            async for event in events:
                if isinstance(event, ai.events.StreamEnd):
                    return event.message.text
    raise RuntimeError("Fx ended before a complete reply")
