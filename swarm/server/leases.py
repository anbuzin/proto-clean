"""Leases: claim, heartbeat, the fence, attempt ends, strikes, `settle`, recovery.

`settle` and `end_attempt` implement the state table in ARCHITECTURE.md section 7.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from swarm import wire

from . import calls, sandboxes, tasks
from .errors import ApiError
from .http import Deps
from .store import Tx, fingerprint


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
    scope = f"worker:{request.worker_id}"
    digest = fingerprint("claim", {}, request.model_dump(mode="json"))

    def find() -> wire.Claim | _Reservation | None:
        with deps.store.write() as tx:
            stored = deps.store.stored_reply(tx, scope, request.request_id, digest)
            if stored:
                attempt = tx.require("SELECT * FROM attempts WHERE worker_id=? AND claim_request=?",
                                     request.worker_id, request.request_id)
                fence(tx, attempt["agent_id"], attempt["attempt"])
                return wire.Claim.model_validate(stored.reply) if stored.reply is not None else None
            return _reserve(tx, deps, request, scope, digest)

    found = deps.store.poll(request.wait_s, find)
    if found is None:
        with deps.store.read() as tx:
            if deps.store.stored_reply(tx, scope, request.request_id, digest) is None:
                return None
        found = find()
        if found is None:
            raise ApiError("busy", "sandbox copy in progress")
    if isinstance(found, wire.Claim):
        return found
    return _publish(deps, request, scope, digest, found)


def heartbeat(deps: Deps, request: wire.Heartbeat, agent_id: str) -> wire.HeartbeatReply:
    """Fence, then `lease_until = mono + ttl`, `activated = 1`. No stored reply, no event.
    The reply carries the server's wall time of `lease_until` only for display."""
    with deps.store.write() as tx:
        fence(tx, agent_id, request.attempt)
        ttl = deps.config.lease_ttl_s
        tx.run("UPDATE agents SET lease_until=? WHERE id=?", tx.mono + ttl, agent_id)
        tx.run("UPDATE attempts SET activated=1 WHERE agent_id=? AND attempt=?", agent_id, request.attempt)
        return wire.HeartbeatReply(lease_until=tx.now + ttl)


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


def end_attempt(tx: Tx, agent_id: str, attempt: int, reason: wire.AttemptEnd) -> str:
    """Close the attempt; clear the lease. `lost`, `server_restart` (I6): `running` unsafe
    calls -> `unknown`. `lost` only: `add_strike`. Then `settle`. Returns the state."""
    agent = tx.require("SELECT * FROM agents WHERE id=?", agent_id)
    tx.run("UPDATE attempts SET ended_at=?, end_reason=? WHERE agent_id=? AND attempt=? AND ended_at IS NULL",
           tx.now, reason, agent_id, attempt)
    tx.run("UPDATE agents SET lease_until=NULL, state=CASE WHEN state='leased' THEN 'ready' ELSE state END "
           "WHERE id=?", agent_id)
    if reason in {"lost", "server_restart", "cancelled"}:
        tx.run("UPDATE calls SET state='unknown' WHERE agent_id=? AND attempt=? AND state='running' "
               "AND safety='unsafe'", agent_id, attempt)
    tx.event(agent["task_id"], agent_id, "attempt_ended", attempt=attempt, reason=reason)
    if reason == "lost":
        add_strike(tx, agent_id, attempt)
    return settle(tx, agent_id)


def add_strike(tx: Tx, agent_id: str, attempt: int) -> None:
    """I10. No strike for a never-activated attempt. A strike if no progress or a local
    call is still `running`; else strikes = 0. `ready_at = now + backoff`. At
    `max_strikes` the agent fails ('poison') unless a call is `unknown` (then `settle`
    gives `blocked`: an open unsafe effect wins over poison)."""
    attempt_row = tx.require("SELECT activated, progress FROM attempts WHERE agent_id=? AND attempt=?",
                             agent_id, attempt)
    if attempt_row["activated"]:
        # I10: a converted unsafe call was still running when this attempt was lost.
        running = tx.one("SELECT 1 FROM calls WHERE agent_id=? AND attempt=? AND "
                         "(state='running' OR (state='unknown' AND safety='unsafe'))", agent_id, attempt)
        tx.run("UPDATE agents SET strikes=CASE WHEN ? THEN strikes+1 ELSE 0 END WHERE id=?",
               not attempt_row["progress"] or running is not None, agent_id)


def settle(tx: Tx, agent_id: str) -> str:
    """State of an agent with no lease, by precedence: terminal stays; a call of the
    pending step `unknown` -> `blocked`; one `waiting` -> `waiting`; else `ready`.
    `planned` calls after a barrier do not count: the next attempt starts them."""
    agent = tx.require("SELECT * FROM agents WHERE id=?", agent_id)
    if agent["state"] in wire.TERMINAL_AGENT_STATES or agent["lease_until"] is not None:
        return agent["state"]
    states = {call.state for call in calls.pending(tx, agent_id)}
    state = "blocked" if "unknown" in states else "waiting" if "waiting" in states else "ready"
    if state != agent["state"]:
        tx.run("UPDATE agents SET state=? WHERE id=?", state, agent_id)
        tx.event(agent["task_id"], agent_id, "agent_settled", state=state)
    return state


def expire_leases(deps: Deps) -> int:
    """Reaper: each `leased` agent with `lease_until <= mono` -> `end_attempt('lost')`."""
    with deps.store.write() as tx:
        expired = tx.all("SELECT id, attempt FROM agents WHERE state='leased' AND lease_until<=?", tx.mono)
        for agent in expired:
            if tx.one("SELECT 1 FROM agents WHERE id=? AND state='leased'", agent["id"]):
                state = end_attempt(tx, agent["id"], agent["attempt"], "lost")
                _apply_backoff(tx, deps, agent["id"], agent["attempt"], state)
        return len(expired)


def recover_after_restart(deps: Deps) -> int:
    """Startup: each open attempt -> `end_attempt('server_restart')` (also a claim whose
    copy was not ready; its partial directory stays unused)."""
    with deps.store.write() as tx:
        agents = tx.all("SELECT id, attempt FROM agents WHERE state='leased'")
        for agent in agents:
            end_attempt(tx, agent["id"], agent["attempt"], "server_restart")
        return len(agents)


@dataclass(frozen=True)
class _Reservation:
    agent_id: str
    attempt: int
    target: Path
    source: Path | None


def _reserve(tx: Tx, deps: Deps, request: wire.ClaimRequest, scope: str, digest: str) -> _Reservation | None:
    if not request.kinds:
        return None
    kinds = ",".join("?" for _ in request.kinds)
    agent = tx.one(f"""SELECT a.* FROM agents a WHERE a.state='ready' AND a.ready_at<=?
        AND a.kind IN ({kinds}) AND (a.seed_agent_id IS NULL OR EXISTS (
            SELECT 1 FROM attempts seed WHERE seed.agent_id=a.seed_agent_id
            AND seed.attempt=a.seed_attempt AND seed.ended_at IS NOT NULL
        )) ORDER BY a.created_at, a.id LIMIT 1""", tx.now, *request.kinds)
    if agent is None:
        return None
    agent_id, attempt = agent["id"], agent["attempt"] + 1
    tx.run("UPDATE agents SET state='leased', attempt=?, lease_until=? WHERE id=?",
           attempt, tx.mono + deps.config.clone_ttl_s, agent_id)
    tx.run("INSERT INTO attempts(agent_id, attempt, worker_id, pid, claim_request, started_at, "
           "sandbox_path, sandbox_state) VALUES(?, ?, ?, ?, ?, ?, '', 'cloning')",
           agent_id, attempt, request.worker_id, request.pid, request.request_id, tx.now)
    target, source = sandboxes.reserve(tx, deps.config.sandboxes_dir, agent, attempt)
    deps.store.save_reply(tx, scope, request.request_id, digest, None)
    tx.event(agent["task_id"], agent_id, "attempt_reserved", attempt=attempt)
    return _Reservation(agent_id, attempt, target, source)


def _publish(deps: Deps, request: wire.ClaimRequest, scope: str, digest: str,
             reservation: _Reservation) -> wire.Claim:
    agent_id, attempt = reservation.agent_id, reservation.attempt
    try:
        sandboxes.materialize(reservation.source, reservation.target)
    except OSError as error:
        with deps.store.write() as tx:
            agent = fence(tx, agent_id, attempt)
            end_attempt(tx, agent_id, attempt, "setup_failed")
            tx.run("UPDATE agents SET clone_tries=clone_tries+1 WHERE id=?", agent_id)
            if agent["clone_tries"] + 1 >= deps.config.max_clone_tries:
                tasks.end_agent(tx, agent_id, "failed", reason="sandbox")
        raise ApiError("stale", str(error)) from error
    with deps.store.write() as tx:
        agent = fence(tx, agent_id, attempt)
        sandboxes.mark_ready(tx, agent_id, attempt)
        ttl = deps.config.lease_ttl_s
        tx.run("UPDATE agents SET lease_until=?, clone_tries=0 WHERE id=?", tx.mono + ttl, agent_id)
        history = tasks.history(tx, agent_id)
        response = wire.Claim(agent_id=agent_id, task_id=agent["task_id"], attempt=attempt, lease_ttl_s=ttl,
                              spec=wire.TaskSpec.model_validate_json(agent["spec_json"]), history=history,
                              next_seq=len(history), pending_calls=calls.pending(tx, agent_id),
                              memory=tasks.memory(tx, agent_id), sandbox_path=str(reservation.target))
        deps.store.save_reply(tx, scope, request.request_id, digest, response.model_dump(mode="json"))
        tx.event(agent["task_id"], agent_id, "attempt_claimed", attempt=attempt)
        return response


def _apply_backoff(tx: Tx, deps: Deps, agent_id: str, attempt: int, state: str) -> None:
    if not tx.require("SELECT activated FROM attempts WHERE agent_id=? AND attempt=?", agent_id, attempt)["activated"]:
        return
    strikes = tx.require("SELECT strikes FROM agents WHERE id=?", agent_id)["strikes"]
    config = deps.config
    delay = min(config.backoff_base_s * 2 ** (strikes - 1), config.backoff_max_s) if strikes else 0
    tx.run("UPDATE agents SET ready_at=? WHERE id=?", tx.now + delay, agent_id)
    if strikes >= config.max_strikes and state != "blocked":
        tasks.end_agent(tx, agent_id, "failed", reason="poison")
