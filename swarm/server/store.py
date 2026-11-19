"""SQLite transactions, stored replies, events, and change notifications."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from swarm.wire import JSON
from .errors import ApiError

T = TypeVar("T")


def json_text(value: Any) -> str:
    """Encode a value as canonical JSON."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class Tx:
    """One transaction, with a fixed wall clock and monotonic clock."""
    def __init__(self, conn: sqlite3.Connection, now: float, mono: float) -> None:
        self.conn, self.now, self.mono = conn, now, mono

    def one(self, sql: str, *params: Any) -> dict | None:
        """Return the first row as a dict, or None."""
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def require(self, sql: str, *params: Any) -> dict:
        """Return the first row or raise not_found."""
        row = self.one(sql, *params)
        if row is None:
            raise ApiError("not_found")
        return row

    def all(self, sql: str, *params: Any) -> list[dict]:
        """Return all rows as dicts."""
        return [dict(row) for row in self.conn.execute(sql, params)]

    def run(self, sql: str, *params: Any) -> int:
        """Execute SQL and return the row count."""
        return self.conn.execute(sql, params).rowcount

    def event(self, task_id: str, agent_id: str | None, type: str, **data: JSON) -> None:
        """Append an event in this transaction."""
        self.run("INSERT INTO events(ts, task_id, agent_id, type, json) VALUES (?, ?, ?, ?, ?)",
                 self.now, task_id, agent_id, type, json_text(data))


@dataclass(frozen=True)
class StoredReply:
    reply: dict[str, JSON] | None
    terminal: bool


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._changed = threading.Condition()
        self._version = 0

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def initialize(self) -> None:
        """Create the schema; repeated initialization is safe."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(Path(__file__).with_name("schema.sql").read_text())
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[Tx]:
        """Commit a write before notifying waiters; roll back on errors."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            before = conn.total_changes
            yield Tx(conn, time.time(), time.monotonic())
            changed = conn.total_changes != before
            conn.commit()
            if changed:
                with self._changed:
                    self._version += 1
                    self._changed.notify_all()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[Tx]:
        """Open a read transaction for a consistent view."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield Tx(conn, time.time(), time.monotonic())
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def stored_reply(self, tx: Tx, scope: str, request_id: str, fingerprint: str) -> StoredReply | None:
        """Look up a reply and reject reuse of an ID for another request."""
        row = tx.one("SELECT * FROM requests WHERE scope=? AND request_id=?", scope, request_id)
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise ApiError("invalid", "request_id reused for a different request")
        return StoredReply(json.loads(row["reply_json"]) if row["reply_json"] is not None else None,
                           bool(row["terminal"]))

    def save_reply(self, tx: Tx, scope: str, request_id: str, fingerprint: str,
                   reply: dict[str, JSON] | None, *, terminal: bool = False) -> None:
        """Insert or complete a reservation in the current transaction."""
        self.stored_reply(tx, scope, request_id, fingerprint)
        tx.run("""INSERT INTO requests(scope, request_id, fingerprint, reply_json, terminal)
                  VALUES (?, ?, ?, ?, ?) ON CONFLICT(scope, request_id) DO UPDATE SET
                  reply_json=excluded.reply_json, terminal=excluded.terminal""",
               scope, request_id, fingerprint, json_text(reply) if reply is not None else None, int(terminal))

    def poll(self, timeout_s: float, attempt: Callable[[], T | None]) -> T | None:
        """Try until ready or timeout, without holding a transaction during waits."""
        deadline = time.monotonic() + max(0, timeout_s)
        while True:
            with self._changed:
                version = self._version
            result = attempt()
            if result is not None:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            with self._changed:
                if self._version == version:
                    self._changed.wait(min(remaining, 0.25))

    def wait_for_change(self, timeout_s: float) -> None:
        """Wait for a write or timeout; this is only a hint."""
        with self._changed:
            self._changed.wait(max(0, timeout_s))

    def close(self) -> None:
        """Close the store; connections are closed at each transaction boundary."""


def fingerprint(route: str, path: dict[str, Any], body: dict[str, JSON]) -> str:
    """Return the SHA-256 hash of the canonical request."""
    return hashlib.sha256(json_text([route, path, body]).encode()).hexdigest()


def new_id(prefix: str) -> str:
    """Return a random prefixed identifier."""
    return f"{prefix}_{secrets.token_hex(16)}"


Clock = Callable[[], float]
