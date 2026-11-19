"""Stored replies shared by owner and agent writes (I3)."""

from dataclasses import dataclass
from typing import Self

from swarm import wire

from .http import Deps
from .store import Tx, fingerprint


@dataclass(frozen=True)
class Reply:
    scope: str
    request_id: str
    fingerprint: str
    route: str
    agent_id: str | None
    attempt: int | None

    @classmethod
    def for_request(cls, route: str, request: wire.OwnerWrite | wire.AgentWrite, **path: str | int) -> Self:
        """Build the request key from the route, path, and validated body."""
        attempt = request.attempt if isinstance(request, wire.AgentWrite) else None
        agent_id = str(path["agent_id"]) if attempt is not None else None
        scope = f"{agent_id}:{attempt}" if attempt is not None else "owner"
        return cls(scope, request.request_id, fingerprint(route, path, request.model_dump(mode="json")),
                   route, agent_id, attempt)

    def read(self, tx: Tx, deps: Deps) -> wire.WireModel | None:
        """Replay an owner, terminal, or current-attempt reply."""
        from .leases import fence

        stored = deps.store.stored_reply(tx, self.scope, self.request_id, self.fingerprint)
        if stored is None:
            return None
        if not stored.terminal and self.agent_id is not None:
            fence(tx, self.agent_id, self.attempt)
        return wire.ROUTE[self.route].reply.model_validate(stored.reply)

    def save[Model: wire.WireModel](self, tx: Tx, deps: Deps, reply: Model, *, terminal: bool = False) -> Model:
        """Save the reply in the transaction that made it."""
        deps.store.save_reply(tx, self.scope, self.request_id, self.fingerprint, reply.model_dump(mode="json"),
                              terminal=terminal)
        return reply
