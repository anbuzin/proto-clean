"""The lease of one claim, as the worker sees it (I15).

Before any work, one heartbeat must succeed. Local deadline = send time of the last
acknowledged heartbeat + TTL (worker monotonic clock); the server's `lease_until` is at
least that late, so a running worker always stops first. A heartbeat thread renews every
TTL/3 (timeout 1 s). If a renewal is refused (`stale`) or the deadline passes with no
acknowledgement, the thread calls `os._exit(3)`: the process stops at once, with no
cleanup that could write. Pause the thread before a call that can release the lease
(`yield`, `finish`, `admit_model_call`), so a late heartbeat cannot kill the next claim.
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
        """The local deadline (monotonic). Each write uses it as its retry deadline."""
        raise NotImplementedError

    def start(self) -> None:
        """First heartbeat (must succeed), then start the renewal thread."""
        raise NotImplementedError

    def check(self) -> None:
        """`os._exit(3)` if the local deadline has passed. Call before each action."""
        raise NotImplementedError

    def pause(self) -> None:
        """Stop and join the renewal thread; the deadline stays."""
        raise NotImplementedError

    def resume(self) -> None:
        """Start the renewal thread again under the current deadline."""
        raise NotImplementedError

    def close(self) -> None:
        """Stop the thread for good. Safe to call twice."""
        raise NotImplementedError
