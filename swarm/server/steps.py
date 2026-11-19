"""Steps of the agent loop: model-call admission and budget, commit step, yield, finish.

Each write: stored-reply check (replay rules in `wire.py`), `fence`, validate, change,
event, stored reply - one transaction. A reply that ends the attempt is stored terminal.
"""

from __future__ import annotations

from swarm import wire

from .http import Deps


def admit_model_call(deps: Deps, request: wire.ModelCallRequest, agent_id: str) -> wire.ModelCallReply:
    """I11. `step_seq` must be the next seq and the last step `user` or `tools`
    (else `conflict`). `used` = sum over the task of `used_tokens`, or of
    `estimated_tokens` where usage is NULL. `used + estimated > budget`: agent `failed`
    ('budget'), attempt `finished`, `on_agent_terminal`, `admitted=False` (terminal).
    Else insert `model_calls` (one per attempt and seq) and return the remaining tokens."""
    raise NotImplementedError


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
    raise NotImplementedError


def yield_agent(deps: Deps, request: wire.AgentWrite, agent_id: str) -> wire.YieldReply:
    """I9. All calls of the pending step `done`: `continue`, lease kept. Else
    `end_attempt('yielded')` and `released` with the settled state (terminal)."""
    raise NotImplementedError


def finish_agent(deps: Deps, request: wire.FinishRequest, agent_id: str) -> wire.AgentStateReply:
    """`done` needs the last step to be a `model` step with no calls. `failed` is refused
    (`conflict`) while a call is `running` unsafe or `unknown`. Agent terminal, attempt
    `finished`, `tasks.on_agent_terminal`. Stored terminal."""
    raise NotImplementedError
