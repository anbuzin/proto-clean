"""Kind `external` (optional module): one turn of a coding CLI (Fx) through `ai.external`.

The CLI turn is one local tool call, `cli_turn`, in the normal step protocol, so the
server needs no special case:
1. History empty: commit the user message (seq 0), then a synthetic model reply with one
   tool call `cli_turn` (call ID `turn-1`). Safety from `spec.external.on_cut`:
   `block` -> `unsafe`, `resume` -> `idempotent`.
2. `start_call` -> `run`: start `ai.external.Fx(cwd=sandbox, resume=checkpoint session)`;
   after `session.start()` and before the prompt, `checkpoint_call({session_id})`.
   Send the prompt (on a resume: "The last turn was cut. Check the sandbox first.").
   Read events to the end of the turn; `finish_call` with the final text.
3. Commit the tool message, then a final assistant message with the text; finish `done`.

A worker kill cuts the turn. `unsafe`: the server makes the call `unknown` and the agent
waits for the owner. `idempotent`: the next attempt runs the call again and resumes the
native session in the new sandbox copy. The CLI gets no server tools in this prototype
(no MCP bridge), and an environment with no model key and no swarm token.
"""

from __future__ import annotations

from swarm import wire

from .lease import Lease

CLI_TURN = "cli_turn"


async def run_external_agent(lease: Lease, claim: wire.Claim) -> None:
    """Run one claim of kind `external` (steps 1 to 3 above)."""
    raise NotImplementedError


def cli_environment() -> dict[str, str]:
    """The CLI environment: an allow list (`PATH`, `HOME`, `LANG`, `TERM`), no
    `AI_GATEWAY_API_KEY`, no `SWARM_*` variable."""
    raise NotImplementedError
