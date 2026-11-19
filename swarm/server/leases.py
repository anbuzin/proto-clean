"""Leases: claim, heartbeat, the fence, attempt ends, strikes, `settle`, recovery.

`settle` and `end_attempt` implement the state table in ARCHITECTURE.md section 7.
"""

from __future__ import annotations

from swarm import wire

from .http import Deps
from .store import Tx


def claim(deps: Deps, request: wire.ClaimRequest) -> wire.Claim | None:
    """Long poll for at most `wait_s`. Replay of a known request ID: find its reserved
    attempt (`attempts.claim_request`) first. Ended (clone failure, expiry, restart):
    `ApiError('stale')`, and the worker starts a new claim with a new request ID. Still
    cloning at the end of `wait_s`: `ApiError('busy')` (503; the client retries the same
    ID). Ready and current: the stored claim.
    1. Reserve, one transaction: oldest `ready` agent with `ready_at <= now`, a kind in
       `kinds`, and, if the agent has no ready copy yet and is a child, its seed attempt
       ended. `attempt += 1`,
       `leased`, `lease_until = mono + clone_ttl_s`, attempts row with `claim_request`,
       `sandboxes.reserve`, stored reply NULL - all in this transaction (I3, I8).
    2. `sandboxes.materialize` with no transaction open.
    3. Publish, one transaction: `fence` for that attempt (else drop the work: `stale`
       for a retry); `mark_ready`; `lease_until = mono + lease_ttl_s`; store the reply.
       A clone error instead: attempt `setup_failed`, `clone_tries += 1`; at
       `max_clone_tries` the agent fails ('sandbox'); else `ready`. No strike.
    Nothing ready by the end of `wait_s`: None (204)."""
    raise NotImplementedError


def heartbeat(deps: Deps, request: wire.Heartbeat, agent_id: str) -> wire.HeartbeatReply:
    """Fence, then `lease_until = mono + ttl`, `activated = 1`. No stored reply, no event.
    The reply carries the server's wall time of `lease_until` only for display."""
    raise NotImplementedError


def fence(tx: Tx, agent_id: str, attempt: int) -> dict:
    """I2. The agent row if `state == 'leased'`, attempt current, `lease_until > tx.mono`;
    else `ApiError('stale')`. No change on refusal."""
    raise NotImplementedError


def mark_progress(tx: Tx, agent_id: str, attempt: int) -> None:
    raise NotImplementedError


def end_attempt(tx: Tx, agent_id: str, attempt: int, reason: wire.AttemptEnd) -> str:
    """Close the attempt; clear the lease. `lost`, `server_restart` (I6): `running` unsafe
    calls -> `unknown`. `lost` only: `add_strike`. Then `settle`. Returns the state."""
    raise NotImplementedError


def add_strike(tx: Tx, agent_id: str, attempt: int) -> None:
    """I10. No strike for a never-activated attempt. A strike if no progress or a local
    call is still `running`; else strikes = 0. `ready_at = now + backoff`. At
    `max_strikes` the agent fails ('poison') unless a call is `unknown` (then `settle`
    gives `blocked`: an open unsafe effect wins over poison)."""
    raise NotImplementedError


def settle(tx: Tx, agent_id: str) -> str:
    """State of an agent with no lease, by precedence: terminal stays; a call of the
    pending step `unknown` -> `blocked`; one `waiting` -> `waiting`; else `ready`.
    `planned` calls after a barrier do not count: the next attempt starts them."""
    raise NotImplementedError


def expire_leases(deps: Deps) -> int:
    """Reaper: each `leased` agent with `lease_until <= mono` -> `end_attempt('lost')`."""
    raise NotImplementedError


def recover_after_restart(deps: Deps) -> int:
    """Startup: each open attempt -> `end_attempt('server_restart')` (also a claim whose
    copy was not ready; its partial directory stays unused)."""
    raise NotImplementedError
