"""Steps of the agent loop: model-call admission and budget, commit step, yield, finish."""

from __future__ import annotations

from swarm import wire

from .store import Store


def admit_model_call(store: Store, auth: str, request: wire.ModelCallRequest,
                     agent_id: str) -> wire.ModelCallReply:
    """I11. Fence. `used` = sum over the task's `model_calls` of `used_tokens`, or of
    `estimated_tokens` where usage is missing. If `used + estimated > budget`: agent
    `failed` ('budget'), attempt `finished`, `on_agent_terminal`, reply `admitted=False`.
    Else insert the `model_calls` row and reply `admitted=True` with the remaining tokens."""
    raise NotImplementedError


def commit_step(store: Store, auth: str, request: wire.StepRequest, agent_id: str) -> wire.StepReply:
    """I4. Fence; `expected_seq` must equal the next seq, else `ApiError('conflict')`.
    One transaction: the message row; one `calls` row per planned call (`planned`);
    usage into the admitted `model_calls` row. Validation: each tool is in the agent's
    spec; tools in `wire.SERVER_TOOLS` have safety `server` and valid args
    (`wire.SERVER_TOOL_ARGS`); other tools do not have safety `server`. A tool message
    (no planned calls, after a model reply) is accepted only when every call of the
    pending step is `done`. `mark_progress`."""
    raise NotImplementedError


def yield_agent(store: Store, auth: str, request: wire.AgentWrite, agent_id: str) -> wire.YieldReply:
    """I9. Fence. If every call of the pending step is `done`: `continue` (the lease stays).
    Else end the attempt `yielded` and `settle` (`waiting` or `blocked`): `released`."""
    raise NotImplementedError


def finish_agent(store: Store, auth: str, request: wire.FinishRequest, agent_id: str) -> wire.AgentStateReply:
    """Fence. `done` needs no open call in the pending step; `failed` is refused while a
    call is `running` with safety `unsafe` or `unknown`. Agent terminal, attempt
    `finished`, `tasks.on_agent_terminal`."""
    raise NotImplementedError
