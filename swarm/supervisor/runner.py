"""Runner: takes launch orders from the server and starts one shim for each.

The runner holds no process state; the server has it. For each `LaunchOrder` it starts
`python -m swarm.supervisor.shim --server URL --launch LAUNCH_ID` in a new session and
writes the order JSON (with the secret) to the shim's stdin, then closes it. The
secret is never in argv or env. The runner does not wait for the shim; it reaps exited
shims only to avoid zombies. A failed `next_launch` is not retried with the same order:
the order expires at its grant deadline (`wire.py` replay rules). If the runner dies,
running shims are not affected.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from swarm import wire
from swarm.client import Rejected, RunnerClient, Unreachable

logger = logging.getLogger(__name__)


class Runner:
    def __init__(self, client: RunnerClient, server_url: str, runner_id: str) -> None:
        """Use the client to get orders for this runner."""
        self._client = client
        self._server_url = server_url
        self._runner_id = runner_id
        self._shims: list[subprocess.Popen] = []

    def launch(self, order: wire.LaunchOrder) -> subprocess.Popen:
        """Start the shim for one order. A spawn error is logged; the server revokes the
        launch at its deadline and the next try gets a new launch.
        """
        data = order.model_dump_json().encode()
        try:
            shim = subprocess.Popen(
                [sys.executable, "-m", "swarm.supervisor.shim", "--server", self._server_url,
                 "--launch", order.launch_id],
                stdin=subprocess.PIPE, start_new_session=True,
            )
            self._shims.append(shim)
            assert shim.stdin is not None
            with shim.stdin as stdin:
                stdin.write(data)
        except OSError as error:
            logger.warning("Could not start shim for %s: %s", order.launch_id, error)
            raise
        return shim

    def run(self, stop: Event) -> None:
        """Until `stop` is set: `next_launch` (long poll), `launch`, reap exited shims.
        On a transport error or 503: back off (0.5 s doubling to 10 s) and start a fresh
        poll; a lost order is never reused or spawned. Reaping goes on while the server
        is down.
        """
        backoff = 0.5
        with ThreadPoolExecutor(max_workers=1) as polls:
            while not stop.is_set():
                poll = polls.submit(self._client.next_launch, self._runner_id, wait_s=1)
                while not poll.done() and not stop.is_set():
                    self._reap()
                    stop.wait(0.2)
                if stop.is_set():
                    break
                try:
                    order = poll.result()
                except (Rejected, Unreachable) as error:
                    logger.warning("Launch poll failed: %s", error)
                    self._wait(stop, backoff)
                    backoff = min(backoff * 2, 10)
                    continue
                backoff = 0.5
                if order is not None:
                    try:
                        self.launch(order)
                    except OSError:
                        pass
                self._reap()
        self._reap()

    def _reap(self) -> None:
        self._shims = [shim for shim in self._shims if shim.poll() is None]

    def _wait(self, stop: Event, delay: float) -> None:
        deadline = time.monotonic() + delay
        while not stop.is_set() and (remaining := deadline - time.monotonic()) > 0:
            self._reap()
            stop.wait(min(remaining, 0.2))
