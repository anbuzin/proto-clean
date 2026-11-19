"""Agent writes that can release the lease; renew only when the lease remains open."""

from collections.abc import Iterator
from contextlib import contextmanager

from ai.types.messages import Message

from swarm import wire
from swarm.client.api import new_request_id

from .lease import Lease


@contextmanager
def _may_release(lease: Lease) -> Iterator[None]:
    lease.pause()
    try:
        yield
    finally:
        lease.check()


def commit_step(lease: Lease, seq: int, kind: wire.StepKind, message: Message, *,
            calls: list[wire.PlannedCall] | None = None, usage: wire.Usage | None = None) -> wire.StepReply:
    """Commit the message and its ordered calls or results."""
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


def admit_model(lease: Lease, seq: int, estimate: int) -> bool:
    """Reserve model budget and return whether work can continue."""
    with _may_release(lease):
        reply = lease.client.admit_model_call(lease.agent_id, lease.attempt, seq, estimate, deadline=lease.deadline)
    if reply.admitted:
        lease.resume()
    return reply.admitted


def yield_agent(lease: Lease) -> bool:
    """Yield for pending calls, or continue if their results arrived in time."""
    with _may_release(lease):
        reply = lease.client.yield_agent(lease.agent_id, lease.attempt, deadline=lease.deadline)
    if reply.action == "continue":
        lease.resume()
        return True
    return False


def finish_agent(lease: Lease, text: str) -> None:
    """Commit the final text and release the attempt."""
    with _may_release(lease):
        lease.client.finish_agent(lease.agent_id, lease.attempt, "done", text, deadline=lease.deadline)
