"""Entry point: `python -m swarm.supervisor --state DIR [--workers N] [--no-runner]
[--worker-command ARGV...]`.

Reads `server.json` (the URL is stable: see `swarm.server.__main__`) and `tokens.json`.
Runs a `WorkerPool` of N workers (default command `python -m swarm.worker --server URL`;
a test gives its own entry script) with `SWARM_WORKER_TOKEN` in their env, and unless
`--no-runner` a `Runner` thread. Holds `DIR/supervisor.lock`. SIGTERM: stop the pool
and the runner; shims and payloads stay.
"""

from __future__ import annotations


import argparse
import fcntl
import json
import logging
import os
import signal
import sys
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from swarm.client import RunnerClient
from swarm.supervisor.pool import WorkerPool
from swarm.supervisor.runner import Runner


def main(argv: list[str] | None = None) -> int:
    """Read the server settings, lock the state directory, and run the supervisor."""
    parser = argparse.ArgumentParser(description="Start a swarm worker pool and command runner.")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--no-runner", action="store_true")
    parser.add_argument("--worker-command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.workers < 0 or args.worker_command == []:
        parser.error("--workers must be nonnegative and --worker-command must not be empty")
    logging.basicConfig(level=logging.INFO)
    try:
        with (args.state / "supervisor.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.error("a supervisor already holds this state directory")
            server = json.loads((args.state / "server.json").read_text())["url"]
            tokens = json.loads((args.state / "tokens.json").read_text())
            _run(server, tokens, args.worker_command, args.workers, args.no_runner)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Could not run supervisor: {error}\n")
    return 0


def _run(server: str, tokens: dict[str, str], command: list[str] | None,
         size: int, no_runner: bool) -> None:
    if os.getpid() != os.getpgrp():
        os.setsid()
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    env = os.environ | {"SWARM_WORKER_TOKEN": tokens["worker"]}
    pool = WorkerPool(command or [sys.executable, "-m", "swarm.worker", "--server", server], size, env)
    if no_runner:
        pool.run(stop)
        return
    runner = Runner(RunnerClient(server, tokens["runner"]), server, uuid4().hex)
    thread = Thread(target=runner.run, args=(stop,), name="runner")
    thread.start()
    try:
        pool.run(stop)
    finally:
        stop.set()
        thread.join()


if __name__ == "__main__":
    raise SystemExit(main())
