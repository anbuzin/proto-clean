"""Launch grants, shim receipts and recovery for process records."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from pathlib import Path

from swarm import wire

from .errors import ApiError
from .deps import Deps
from .processes import on_process_end
from .store import Tx, json_text, new_id


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _body(request: wire.WireModel) -> str:
    return _hash(json_text(request.model_dump(mode="json")))


def next_launch(deps: Deps, request: wire.NextLaunchRequest) -> wire.LaunchOrder | None:
    """Long-poll for the oldest request and issue a launch with a secret and deadline."""
    def issue() -> wire.LaunchOrder | None:
        with deps.store.write() as tx:
            row = tx.one("SELECT * FROM processes WHERE state='requested' AND desired='running' ORDER BY created_at, id LIMIT 1")
            if row is None:
                return None
            launch_id, secret = new_id("launch"), secrets.token_urlsafe(32)
            tx.run("""INSERT INTO launches (id, process_id, secret_hash, state, grant_deadline, issued_at)
                      VALUES (?, ?, ?, 'issued', ?, ?)""", launch_id, row["id"], _hash(secret),
                   tx.mono + deps.config.grant_deadline_s, tx.now)
            tx.run("UPDATE processes SET state='launching', tries=tries+1, current_launch=? WHERE id=?",
                   launch_id, row["id"])
            tx.event(row["task_id"], row["agent_id"], "launch_issued", process_id=row["id"], launch_id=launch_id)
            env = json.loads(row["env_json"])
            return wire.LaunchOrder(launch_id=launch_id, process_id=row["id"], secret=secret,
                argv=json.loads(row["argv_json"]), cwd=row["cwd"], env=env, log_path=row["log_path"],
                log_cap_bytes=deps.config.log_cap_bytes, private_dirs=[env["HOME"], env["TMPDIR"]],
                grant_deadline_s=deps.config.grant_deadline_s)
    return deps.store.poll(request.wait_s, issue)


def authenticate_launch(tx: Tx, launch_id: str, secret: str | None) -> dict:
    """Check the launch secret inside the caller's transaction, or refuse it."""
    row = tx.one("SELECT * FROM launches WHERE id=?", launch_id)
    if row is None or secret is None or not hmac.compare_digest(row["secret_hash"], _hash(secret)):
        raise ApiError("unauthorized", "invalid launch secret")
    return row


def grant_launch(deps: Deps, request: wire.GrantRequest, launch_id: str) -> wire.GrantReply:
    """Grant only a current, unexpired launch with desired state running.

    The same grant body replays successfully; a different body or a revoked launch fails.
    """
    with deps.store.write() as tx:
        launch = authenticate_launch(tx, launch_id, deps.launch_secret)
        row = tx.require("SELECT * FROM processes WHERE id=?", launch["process_id"])
        body = _body(request)
        if launch["state"] == "granted":
            return wire.GrantReply(granted=launch["grant_hash"] == body and row["current_launch"] == launch_id)
        if (launch["state"] != "issued" or row["current_launch"] != launch_id or
                launch["grant_deadline"] <= tx.mono or row["desired"] != "running"):
            return wire.GrantReply(granted=False)
        tx.run("UPDATE launches SET state='granted', pid=?, grant_hash=?, heartbeat_mono=? WHERE id=?",
               request.pid, body, tx.mono, launch_id)
        tx.run("UPDATE processes SET state='running', granted_at=? WHERE id=?", tx.now, row["id"])
        tx.event(row["task_id"], row["agent_id"], "launch_granted", process_id=row["id"], launch_id=launch_id)
        return wire.GrantReply(granted=True)


def launch_heartbeat(deps: Deps, request: wire.LaunchHeartbeat, launch_id: str) -> wire.LaunchHeartbeatReply:
    """Record shim progress and request stop for a cut, timeout, or noncurrent launch."""
    with deps.store.write() as tx:
        launch = authenticate_launch(tx, launch_id, deps.launch_secret)
        row = tx.require("SELECT * FROM processes WHERE id=?", launch["process_id"])
        if launch["state"] != "granted" or row["current_launch"] != launch_id:
            return wire.LaunchHeartbeatReply(stop=True)
        tx.run("UPDATE launches SET heartbeat_mono=? WHERE id=?", tx.mono, launch_id)
        tx.run("UPDATE processes SET log_bytes=?, truncated_bytes=? WHERE id=?",
               request.log_bytes, request.truncated_bytes, row["id"])
        stop = row["desired"] == "stopped"
        if not stop and row["lifetime"] == "command" and tx.now >= row["granted_at"] + row["timeout_s"]:
            tx.run("UPDATE processes SET desired='stopped', stop_reason='timeout' WHERE id=?", row["id"])
            tx.event(row["task_id"], row["agent_id"], "process_stop_requested", process_id=row["id"], reason="timeout")
            stop = True
        return wire.LaunchHeartbeatReply(stop=stop)


def _retry_or_end(tx: Tx, deps: Deps, row: dict) -> None:
    if row["desired"] == "stopped":
        state = "ended"
    elif row["tries"] >= deps.config.launch_tries:
        state = "failed"
    else:
        state = "requested"
    if state == "requested":
        tx.run("UPDATE processes SET state='requested', current_launch=NULL WHERE id=?", row["id"])
    else:
        tx.run("UPDATE processes SET state=?, outcome='not_started', ended_at=? WHERE id=?", state, tx.now, row["id"])
        on_process_end(tx, row["id"])
    tx.event(row["task_id"], row["agent_id"], "process_" + state, process_id=row["id"])


def _valid_exit(request: wire.ExitReport) -> bool:
    if request.log_bytes < 0 or request.truncated_bytes < 0:
        return False
    if request.started:
        if (request.exit_code is None) == (request.signal is None):
            return False
        if request.exit_code is not None and request.exit_code < 0:
            return False
        if request.signal is not None and request.signal <= 0:
            return False
    return True


def _apply_exit(tx: Tx, deps: Deps, launch: dict, request: wire.ExitReport) -> wire.Ack:
    if not _valid_exit(request):
        raise ApiError("invalid", "invalid exit report")
    digest = _body(request)
    if launch["state"] == "ended":
        if launch["exit_hash"] == digest:
            return wire.Ack()
        raise ApiError("conflict", "launch already ended with another report")
    row = tx.require("SELECT * FROM processes WHERE id=?", launch["process_id"])
    current = row["current_launch"] == launch["id"]
    if request.started and (launch["state"] != "granted" or not current):
        raise ApiError("conflict", "launch was not granted")
    if not request.started and launch["state"] == "granted":
        raise ApiError("conflict", "granted launch cannot be denied")
    tx.run("UPDATE launches SET state='ended', exit_hash=?, exit_json=?, ended_at=? WHERE id=?",
           digest, json_text(request.model_dump(mode="json")), tx.now, launch["id"])
    if current and not request.started and launch["state"] == "issued":
        _retry_or_end(tx, deps, row)
    elif current and request.started:
        outcome = "signaled" if request.signal is not None else "exited"
        tx.run("""UPDATE processes SET state='ended', outcome=?, exit_code=?, signal=?,
                   log_bytes=?, truncated_bytes=?, ended_at=? WHERE id=?""", outcome,
               request.exit_code, request.signal, request.log_bytes, request.truncated_bytes, tx.now, row["id"])
        tx.event(row["task_id"], row["agent_id"], "process_ended", process_id=row["id"], outcome=outcome)
        on_process_end(tx, row["id"])
    return wire.Ack()


def report_exit(deps: Deps, request: wire.ExitReport, launch_id: str) -> wire.Ack:
    """Apply a denial or granted exit; replay only an identical receipt.

    A current denial retries or ends the process. A granted exit ends the process
    and fills its waiter. A late denial never changes a noncurrent process.
    """
    with deps.store.write() as tx:
        return _apply_exit(tx, deps, authenticate_launch(tx, launch_id, deps.launch_secret), request)


def _revoke(tx: Tx, deps: Deps, launch: dict) -> None:
    tx.run("UPDATE launches SET state='revoked', ended_at=? WHERE id=?", tx.now, launch["id"])
    row = tx.require("SELECT * FROM processes WHERE id=?", launch["process_id"])
    if row["current_launch"] == launch["id"]:
        _retry_or_end(tx, deps, row)


def expire_launches(deps: Deps) -> None:
    """Revoke expired grants and reconcile silent shims from receipts or unknown.

    An issued launch retries until the try limit. A granted launch is never retried;
    without a valid receipt its final log size comes from the file.
    """
    with deps.store.write() as tx:
        for launch in tx.all("SELECT * FROM launches WHERE state='issued' AND grant_deadline<=?", tx.mono):
            _revoke(tx, deps, launch)
        silent = tx.all("""SELECT l.*, p.log_path FROM launches l JOIN processes p ON p.id=l.process_id
                           WHERE l.state='granted' AND l.heartbeat_mono<=?""", tx.mono - deps.config.shim_silence_s)
    # Read receipts and log sizes without holding a database transaction.
    for snapshot in silent:
        log = Path(snapshot["log_path"])
        receipt = None
        try:
            receipt = wire.ExitReport.model_validate_json(log.with_name("exit.json").read_text())
            if not receipt.started or not _valid_exit(receipt):
                receipt = None
        except (OSError, ValueError):
            pass
        try:
            size = log.stat().st_size
        except OSError:
            size = 0
        with deps.store.write() as tx:
            launch = tx.one("SELECT * FROM launches WHERE id=?", snapshot["id"])
            if not launch or launch["state"] != "granted" or launch["heartbeat_mono"] != snapshot["heartbeat_mono"]:
                continue
            row = tx.require("SELECT * FROM processes WHERE id=?", launch["process_id"])
            if row["current_launch"] != launch["id"]:
                continue
            if receipt is not None:
                _apply_exit(tx, deps, launch, receipt)
            else:
                tx.run("UPDATE launches SET state='ended', ended_at=? WHERE id=?", tx.now, launch["id"])
                tx.run("UPDATE processes SET state='ended', outcome='unknown', log_bytes=?, ended_at=? WHERE id=?",
                       size, tx.now, row["id"])
                tx.event(row["task_id"], row["agent_id"], "process_ended", process_id=row["id"], outcome="unknown")
                on_process_end(tx, row["id"])


def enforce_lifetimes(deps: Deps) -> None:
    """End stopped requests that have not been granted, and revoke their launch."""
    with deps.store.write() as tx:
        rows = tx.all("SELECT * FROM processes WHERE desired='stopped' AND state IN ('requested', 'launching')")
        for row in rows:
            tx.run("UPDATE launches SET state='revoked', ended_at=? WHERE id=? AND state='issued'", tx.now, row["current_launch"])
            tx.run("UPDATE processes SET state='ended', outcome='not_started', ended_at=? WHERE id=?", tx.now, row["id"])
            tx.event(row["task_id"], row["agent_id"], "process_ended", process_id=row["id"], outcome="not_started")
            on_process_end(tx, row["id"])


def recover_after_restart(deps: Deps) -> None:
    """Revoke issued launches and give granted shims a new heartbeat window."""
    with deps.store.write() as tx:
        for launch in tx.all("SELECT * FROM launches WHERE state='issued'"):
            _revoke(tx, deps, launch)
        tx.run("UPDATE launches SET heartbeat_mono=? WHERE state='granted'", tx.mono)
