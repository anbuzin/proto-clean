"""Start one shim per launch order; keep its secret on stdin and reap its exit.

The runner starts no payload itself. If it loses an order, the server revokes
that order at its deadline. Shims outlive the runner.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from queue import Empty, Queue
from threading import Event, Thread

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
        while not stop.is_set():
            replies: Queue[wire.LaunchOrder | None | Exception] = Queue()
            # A long poll must not hold up shim reaping or supervisor shutdown.
            Thread(target=self._poll, args=(replies,), daemon=True).start()
            while not stop.is_set():
                self._reap()
                try:
                    order = replies.get(timeout=0.2)
                    break
                except Empty:
                    continue
            if stop.is_set():
                break
            if isinstance(order, (Rejected, Unreachable)):
                logger.warning("Launch poll failed: %s", order)
                self._wait(stop, backoff)
                backoff = min(backoff * 2, 10)
                continue
            if isinstance(order, Exception):
                raise order
            backoff = 0.5
            if order is not None:
                try:
                    self.launch(order)
                except OSError:
                    pass
        self._reap()

    def _poll(self, replies: Queue[wire.LaunchOrder | None | Exception]) -> None:
        try:
            replies.put(self._client.next_launch(self._runner_id, wait_s=10))
        except Exception as error:
            replies.put(error)

    def _reap(self) -> None:
        for shim in self._shims[:]:
            status = shim.poll()
            if status is not None:
                self._shims.remove(shim)
                log = logger.warning if status else logger.info
                log("Shim %d exited with status %d", shim.pid, status)

    def _wait(self, stop: Event, delay: float) -> None:
        deadline = time.monotonic() + delay
        while not stop.is_set() and (remaining := deadline - time.monotonic()) > 0:
            self._reap()
            stop.wait(min(remaining, 0.2))
