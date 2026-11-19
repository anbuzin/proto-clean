"""The lease of one claim, as the worker sees it (I15).

Before any work, one heartbeat must succeed. Local deadline = send time of the last
acknowledged heartbeat + TTL (worker `time.monotonic()`); an ack that arrives after the
deadline does not count. The server's lease ends later (it starts at receive time), so a
running worker stops first. The thread renews every TTL/3 with a 1 s timeout. A `stale`
reply or a passed deadline: `os._exit(3)`, with no cleanup that could write.

Rule for calls that can release the lease (`admit_model_call`, `commit_step` of kind
`model`, `yield_agent`, `finish_agent`): `pause()` first; make the call with
`deadline=lease.deadline`; if the reply keeps the lease (`admitted`, step state
`leased`, `continue`) `resume()`; else return from the claim. Thus a late heartbeat
never runs against a released lease.
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
