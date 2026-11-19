"""Keep a local deadline ahead of the server's lease expiry (I15).

An acknowledged heartbeat sets the deadline from its send time, not its
arrival. Before a write that can release the lease, pause renewal; resume only
when the reply keeps the lease. A late reply cannot revive an expired lease.
"""

from __future__ import annotations

import logging
import os
from threading import Event, Thread
from time import monotonic, sleep
from typing import NoReturn

from swarm import wire
from swarm.client import AgentClient, Rejected, Unreachable


class Lease:
    def __init__(self, client: AgentClient, claim: wire.Claim) -> None:
        self.client = client
        self.agent_id = claim.agent_id
        self.attempt = claim.attempt
        self._ttl = claim.lease_ttl_s
        self._deadline = monotonic() + self._ttl
        self._stop = Event()
        self._thread: Thread | None = None

    @property
    def deadline(self) -> float:
        """Return the local deadline from the latest acknowledged heartbeat."""
        return self._deadline

    def start(self) -> None:
        """First heartbeat (must succeed), then the renewal thread."""
        self.resume()

    def check(self) -> None:
        """`os._exit(3)` if the local deadline has passed. Call before each action."""
        if monotonic() >= self._deadline:
            self.stop("local deadline passed")

    def stop(self, reason: str) -> NoReturn:
        """Stop without cleanup that could write after lease loss."""
        logging.getLogger(__name__).error("Stopping %s attempt %d: %s", self.agent_id, self.attempt, reason)
        os._exit(3)

    def pause(self) -> None:
        """Stop and join the renewal thread; the deadline stays."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self.check()

    def resume(self) -> None:
        """Retry renewal within the current deadline before starting the thread."""
        while not self._heartbeat():
            sleep(max(0, min(0.1, self._deadline - monotonic())))
        self._stop.clear()
        self._thread = Thread(target=self._renew, name="lease-renewal", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop the thread for good. Safe to call twice."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _heartbeat(self) -> bool:
        self.check()
        sent = monotonic()
        try:
            self.client.heartbeat(self.agent_id, self.attempt, timeout_s=min(1, self._deadline - sent))
        except Rejected as error:
            if error.stale or error.status != 503:
                self.stop(f"heartbeat rejected: {error}")
            return False
        except Unreachable:
            return False
        # I15: a late reply cannot extend a deadline that already passed.
        self.check()
        self._deadline = sent + self._ttl
        return True

    def _renew(self) -> None:
        interval = self._ttl / 3
        delay = interval
        while not self._stop.wait(max(0, min(delay, self._deadline - monotonic()))):
            try:
                renewed = self._heartbeat()
            except Exception as error:
                self.stop(f"heartbeat failed: {error}")
            delay = interval if renewed else min(0.1, interval)
