"""Read stored replies before fencing agent writes (I2, I3)."""

from dataclasses import dataclass
from typing import Self

from swarm import wire

from .deps import Deps
from .errors import ApiError
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


def fence(tx: Tx, agent_id: str, attempt: int) -> dict:
    """I2. The agent row if `state == 'leased'`, attempt current, `lease_until > tx.mono`;
    else `ApiError('stale')`. No change on refusal."""
    agent = tx.one("SELECT * FROM agents WHERE id=?", agent_id)
    if (agent is None or agent["state"] != "leased" or agent["attempt"] != attempt
            or agent["lease_until"] is None or agent["lease_until"] <= tx.mono):
        raise ApiError("stale")
    return agent


def mark_progress(tx: Tx, agent_id: str, attempt: int) -> None:
    """Record useful work for the strike rule (I10)."""
    tx.run("UPDATE attempts SET progress=1 WHERE agent_id=? AND attempt=?", agent_id, attempt)
