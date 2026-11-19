"""The lease of one claim, as the worker sees it (I15).

Before any work, one heartbeat must succeed. Local deadline = send time of the last
acknowledged heartbeat + TTL (worker `time.monotonic()`); an ack that arrives after the
deadline does not count. The server's lease ends later (it starts at receive time), so a
running worker stops first. The thread renews every TTL/3 with a 1 s timeout. A `stale`
reply or a passed deadline: `os._exit(3)`, with no cleanup that could write.

Rule for calls that can release the lease (`admit_model_call`, `commit_step` of kind
`model`, `yield_agent`, `finish_agent`): `pause()` first; make the call with
`deadline=lease.deadline`; if the reply keeps the lease (`admitted`, step state
`leased`, `continue`) `resume()`; else return from the claim. Thus a late heartbeat
never runs against a released lease.
"""

from __future__ import annotations

from swarm import wire
from swarm.client import AgentClient


class Lease:
    def __init__(self, client: AgentClient, claim: wire.Claim) -> None:
        self.client = client
        self.claim = claim
        self.agent_id = claim.agent_id
        self.attempt = claim.attempt
        raise NotImplementedError

    @property
    def deadline(self) -> float:
        raise NotImplementedError

    def start(self) -> None:
        """First heartbeat (must succeed), then the renewal thread."""
        raise NotImplementedError

    def check(self) -> None:
        """`os._exit(3)` if the local deadline has passed. Call before each action."""
        raise NotImplementedError

    def pause(self) -> None:
        """Stop and join the renewal thread; the deadline stays."""
        raise NotImplementedError

    def resume(self) -> None:
        """One heartbeat now (it must succeed), then the renewal thread again."""
        raise NotImplementedError

    def close(self) -> None:
        """Stop the thread for good. Safe to call twice."""
        raise NotImplementedError
