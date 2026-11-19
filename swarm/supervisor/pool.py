"""Worker pool: N slots, each runs one worker process; a dead worker is started again.

Each worker starts with `start_new_session=True`, so a kill of one process group does
not reach the supervisor or the server. Restart backoff per slot: 0.5 s doubling to
30 s after quick exits (less than 10 s of life); reset after a long life. The pool knows
nothing of agents: retry, strikes, and poison are server rules (I10).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class Slot:
    index: int
    process: subprocess.Popen | None = None
    started_at: float = 0.0
    failures: int = 0
    next_start: float = 0.0


class WorkerPool:
    def __init__(self, command: list[str], size: int, env: dict[str, str]) -> None:
        """`command` is the full worker argv; `env` is the full environment (with the token)."""
        raise NotImplementedError

    def tick(self, now: float) -> None:
        """Reap exited workers (`poll`), start due slots."""
        raise NotImplementedError

    def run(self, stop) -> None:
        """`tick` every 0.2 s until `stop` (a `threading.Event`) is set; then `close`."""
        raise NotImplementedError

    def close(self, grace_s: float = 2.0) -> None:
        """TERM each worker group, KILL after `grace_s`, reap."""
        raise NotImplementedError

    def pids(self) -> list[int]:
        raise NotImplementedError
