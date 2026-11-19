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

import subprocess

from swarm import wire
from swarm.client import RunnerClient


class Runner:
    def __init__(self, client: RunnerClient, server_url: str, runner_id: str) -> None:
        raise NotImplementedError

    def launch(self, order: wire.LaunchOrder) -> subprocess.Popen:
        """Start the shim for one order. A spawn error is logged; the server revokes the
        launch at its deadline and the next try gets a new launch."""
        raise NotImplementedError

    def run(self, stop) -> None:
        """Until `stop` is set: `next_launch` (long poll), `launch`, reap exited shims.
        On a transport error or 503: back off (0.5 s doubling to 10 s) and start a fresh
        poll; a lost order is never reused or spawned. Reaping goes on while the server
        is down."""
        raise NotImplementedError
