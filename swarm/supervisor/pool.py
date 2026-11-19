"""Worker pool: N slots, each runs one worker process; a dead worker is started again.

Each worker starts with `start_new_session=True`, so a kill of one process group does
not reach the supervisor or the server. Restart backoff per slot: 0.5 s doubling to
30 s after quick exits (less than 10 s of life); reset after a long life. The pool knows
nothing of agents: retry, strikes, and poison are server rules (I10).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from threading import Event

logger = logging.getLogger(__name__)


@dataclass
class Slot:
    index: int
    process: subprocess.Popen | None = None
    started_at: float = 0.0
    failures: int = 0
    next_start: float = 0.0

    def _delay_restart(self, now: float) -> None:
        self.failures = min(self.failures + 1, 7)
        self.next_start = now + min(0.5 * 2 ** (self.failures - 1), 30)


def _signal_worker(process: subprocess.Popen, sig: signal.Signals) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass


class WorkerPool:
    def __init__(self, command: list[str], size: int, env: dict[str, str]) -> None:
        """`command` is the full worker argv; `env` is the full environment (with the token)."""
        self._command = command
        self._env = env
        self._slots = [Slot(index) for index in range(size)]

    def tick(self, now: float) -> None:
        """Reap exited workers (`poll`), start due slots."""
        for slot in self._slots:
            if slot.process is not None:
                if slot.process.poll() is None:
                    continue
                slot.process = None
                if now - slot.started_at < 10:
                    slot._delay_restart(now)
                else:
                    slot.failures = 0
            if now >= slot.next_start:
                try:
                    slot.process = subprocess.Popen(
                        self._command, env=self._env, start_new_session=True,
                        stdin=subprocess.DEVNULL,
                    )
                except OSError as error:
                    logger.warning("Could not start worker in slot %d: %s", slot.index, error)
                    slot._delay_restart(now)
                else:
                    slot.started_at = now

    def run(self, stop: Event) -> None:
        """`tick` every 0.2 s until `stop` (a `threading.Event`) is set; then `close`."""
        try:
            while not stop.is_set():
                self.tick(time.monotonic())
                stop.wait(0.2)
        finally:
            self.close()

    def close(self, grace_s: float = 2.0) -> None:
        """TERM each worker group, KILL after `grace_s`, reap."""
        processes = [slot.process for slot in self._slots if slot.process is not None]
        for process in processes:
            _signal_worker(process, signal.SIGTERM)
        deadline = time.monotonic() + grace_s
        for process in processes:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _signal_worker(process, signal.SIGKILL)
                process.wait()
        for slot in self._slots:
            slot.process = None

    def pids(self) -> list[int]:
        """Return the PIDs of the workers that are still running."""
        return [slot.process.pid for slot in self._slots
                if slot.process is not None and slot.process.poll() is None]
