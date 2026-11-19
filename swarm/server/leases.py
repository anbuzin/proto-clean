"""Leases: claim, heartbeat, the fence, expiry, strikes, `settle`, restart recovery.

The fence (I2) is the core rule of the primitive: every agent write calls `fence()`
first, inside its `BEGIN IMMEDIATE` transaction.
"""

from __future__ import annotations

from swarm import wire

from .store import Store, Tx


def claim(store: Store, auth: str, request: wire.ClaimRequest) -> wire.Claim | None:
    """Long poll for at most `wait_s`. Each try is one transaction (I3, scope
    `worker:<worker_id>`): pick the oldest `ready` agent with `ready_at <= now` and a kind
    in `kinds`; `attempt += 1`; state `leased`; `lease_until = now + ttl`; `attempts` row;
    reserve the sandbox copy (`sandboxes.reserve`). Then clone with no transaction open
    (`sandboxes.materialize`), then commit `ready` and the stored reply (I8).
    Between tries: `store.wait_for_change(0.5)`. Nothing ready by the end: `None` (204)."""
    raise NotImplementedError


def heartbeat(store: Store, auth: str, request: wire.Heartbeat, agent_id: str) -> wire.HeartbeatReply:
    """Fence, then `lease_until = now + ttl`. No `requests` row, no event."""
    raise NotImplementedError


def fence(tx: Tx, agent_id: str, attempt: int) -> dict:
    """I2. Return the agent row if `state == 'leased'`, `attempt` equals the row's
    attempt, and `lease_until > tx.now`; else raise `ApiError('stale')`. No change on refusal."""
    raise NotImplementedError


def mark_progress(tx: Tx, agent_id: str, attempt: int) -> None:
    """Set `attempts.progress = 1` (a committed step or call result in this attempt)."""
    raise NotImplementedError


def end_attempt(tx: Tx, agent_id: str, attempt: int, reason: wire.AttemptEnd) -> None:
    """Close the attempt row and clear the lease. For `lost` and `server_restart` (I6):
    each `running` call with safety `unsafe` becomes `unknown`; `idempotent` ones stay
    `running` (the next attempt gets `run` again). For `lost` only: `add_strike` (I10).
    Then `settle()`."""
    raise NotImplementedError


def add_strike(tx: Tx, agent_id: str, attempt: int) -> None:
    """I10. A lost attempt with no progress, or with a local call still `running`, adds a
    strike; else strikes = 0. `ready_at = now + backoff`. At `max_strikes`: agent `failed`
    ('poison') and `tasks.on_agent_terminal`."""
    raise NotImplementedError


def settle(tx: Tx, agent_id: str) -> str:
    """The state of an agent that has no lease. Terminal stays. A call of the pending step
    `unknown` -> `blocked`; else one `waiting` -> `waiting`; else `ready`. Returns the state."""
    raise NotImplementedError


def expire_leases(store: Store) -> int:
    """Reaper: each `leased` agent with `lease_until <= now` -> `end_attempt(..., 'lost')`.
    One transaction per agent. Returns the count."""
    raise NotImplementedError


def recover_after_restart(store: Store) -> int:
    """Startup, before listening: each open attempt -> `end_attempt(..., 'server_restart')`."""
    raise NotImplementedError
