"""SQLite store: connections, transactions, stored replies, events, wake-ups.

Boundary: the store owns transactions, not SQL. Each domain module writes the SQL for
its own tables through `Tx`; no module opens a connection itself.
Rules (I1, I3): each write runs in `BEGIN IMMEDIATE ... COMMIT` on a connection of its
own thread; the HTTP layer replies only after `write()` returns. No transaction stays
open during a long-poll wait or a file operation. Pragmas: WAL, `synchronous=FULL`,
`foreign_keys=ON`, `busy_timeout=5000`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from swarm.wire import JSON


class Tx:
    """One open transaction. `now` is wall time, `mono` is `time.monotonic()`: one value
    each for the whole transaction."""

    def __init__(self, conn: sqlite3.Connection, now: float, mono: float) -> None:
        self.conn = conn
        self.now = now
        self.mono = mono

    def one(self, sql: str, *params: Any) -> dict | None:
        """First row as a dict, or None."""
        raise NotImplementedError

    def all(self, sql: str, *params: Any) -> list[dict]:
        raise NotImplementedError

    def run(self, sql: str, *params: Any) -> int:
        """Execute; return the row count."""
        raise NotImplementedError

    def event(self, task_id: str, agent_id: str | None, type: str, **data: JSON) -> None:
        """Append one `events` row. Each state change writes one in the same transaction."""
        raise NotImplementedError


@dataclass(frozen=True)
class StoredReply:
    reply: dict[str, JSON] | None     # None: a claim reservation whose copy is not ready yet
    terminal: bool


class Store:
    def __init__(self, db_path: Path) -> None:
        raise NotImplementedError

    def initialize(self) -> None:
        """Create the schema from `schema.sql` (it is safe to run again)."""
        raise NotImplementedError

    @contextmanager
    def write(self) -> Iterator[Tx]:
        """`BEGIN IMMEDIATE`; commit on exit; roll back on any exception; then notify waiters."""
        raise NotImplementedError
        yield  # pragma: no cover

    @contextmanager
    def read(self) -> Iterator[Tx]:
        """A read transaction (`BEGIN`), for owner views."""
        raise NotImplementedError
        yield  # pragma: no cover

    def stored_reply(self, tx: Tx, scope: str, request_id: str, fingerprint: str) -> StoredReply | None:
        """I3. The stored reply, or None if the ID is new. Another fingerprint: `ApiError('invalid')`.
        The caller decides about replay (rules in `wire.py` header)."""
        raise NotImplementedError

    def save_reply(self, tx: Tx, scope: str, request_id: str, fingerprint: str,
                   reply: dict[str, JSON] | None, *, terminal: bool = False) -> None:
        """Insert or complete the stored reply in this transaction."""
        raise NotImplementedError

    def wait_for_change(self, timeout_s: float) -> None:
        """Block until a write commits or the timeout ends. A hint only; callers read again."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def fingerprint(route: str, path: dict[str, Any], body: dict[str, JSON]) -> str:
    """sha256 of the canonical JSON of [route, path, body]."""
    raise NotImplementedError


def new_id(prefix: str) -> str:
    """A short random ID such as `ag_3f9c...`; prefixes: task, ag, proc, launch."""
    raise NotImplementedError


Clock = Callable[[], float]
