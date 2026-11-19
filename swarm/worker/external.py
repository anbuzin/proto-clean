"""Kind `external`: one turn of a coding CLI (Fx) through `ai.external`. Minimal form.

The turn is one `unsafe` tool call, `cli_turn` (call ID `turn`), in the normal step
protocol. The server accepts `cli_turn` only for kind `external`, as the only call.
1. History empty: commit kind `user` (seq 0).
2. `admit_model_call` with `spec.external.turn_tokens`; commit kind `model`: a synthetic
   `ai.assistant_message` with one tool call `cli_turn` (args `{}`).
3. `start_call` -> `run`: `async with ai.external.Fx(cwd=sandbox, ...) as harness`;
   `await harness.session.send(ai.user_message(spec.prompt))`; read
   `harness.session.events()` and stop at the first `ai.events.StreamEnd`;
   `finish_call(done)` with the final text. Any error: `finish_call(unknown)`.
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

from swarm import wire

from .lease import Lease

CLI_TURN = "cli_turn"


async def run_external_agent(lease: Lease, claim: wire.Claim) -> None:
    raise NotImplementedError
