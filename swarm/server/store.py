"""SQLite store: connections, transactions, idempotent replies, events, wake-ups.

Rules (I1, I3): each write runs in `BEGIN IMMEDIATE ... COMMIT` on its own connection;
the HTTP layer replies only after `write()` returns. No transaction stays open during
a long-poll wait, a file operation, or a network write. Each thread has its own
connection (`synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=5000`, WAL).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from swarm.wire import JSON, ErrorReply


class ApiError(Exception):
    """A refusal that the HTTP layer turns into `wire.ErrorReply` with `wire.ERROR_STATUS`."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail

    def reply(self) -> ErrorReply:
        return ErrorReply(error=self.kind, detail=self.detail)  # type: ignore[arg-type]


class Tx:
    """One open transaction. Thin helpers over `sqlite3.Connection`; rows are `sqlite3.Row`."""

    def __init__(self, conn: sqlite3.Connection, now: float) -> None:
        self.conn = conn
        self.now = now                  # one clock value for the whole transaction

    def one(self, sql: str, *params: Any) -> sqlite3.Row | None:
        raise NotImplementedError

    def all(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        raise NotImplementedError

    def run(self, sql: str, *params: Any) -> int:
        """Execute; return the row count."""
        raise NotImplementedError

    def event(self, task_id: str, agent_id: str | None, type: str, **data: JSON) -> None:
        """Append one `events` row. Each state change writes one in the same transaction."""
        raise NotImplementedError


class Store:
    def __init__(self, db_path: Path, clock: Callable[[], float] | None = None) -> None:
        raise NotImplementedError

    def initialize(self) -> None:
        """Create the schema from `schema.sql`; set WAL mode."""
        raise NotImplementedError

    @contextmanager
    def write(self) -> Iterator[Tx]:
        """`BEGIN IMMEDIATE`; commit on exit; roll back on any exception. Notifies waiters."""
        raise NotImplementedError
        yield  # pragma: no cover

    @contextmanager
    def read(self) -> Iterator[Tx]:
        """A read transaction (`BEGIN`), for owner views."""
        raise NotImplementedError
        yield  # pragma: no cover

    def idempotent(self, tx: Tx, scope: str, request_id: str, payload: dict[str, JSON],
                   compute: Callable[[], dict[str, JSON]]) -> dict[str, JSON]:
        """I3. If (scope, request_id) exists: same payload hash -> stored reply; else
        `ApiError('invalid')`. If not: run `compute()` and store its reply in this `tx`."""
        raise NotImplementedError

    def wait_for_change(self, timeout_s: float) -> None:
        """Block until a write commits or the timeout ends. A hint only; callers re-read."""
        raise NotImplementedError

    def now(self) -> float:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def new_id(prefix: str) -> str:
    """A short random ID such as `ag_3f9c...`; prefixes: task, ag, proc, launch."""
    raise NotImplementedError
