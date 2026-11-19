"""Entry point: `python -m swarm.supervisor --state DIR [--workers N] [--no-runner]
[--worker-command ARGV...]`.

Reads `server.json` (the URL is stable: see `swarm.server.__main__`) and `tokens.json`.
Runs a `WorkerPool` of N workers (default command `python -m swarm.worker --server URL`;
a test gives its own entry script) with `SWARM_WORKER_TOKEN` in their env, and unless
`--no-runner` a `Runner` thread. Holds `DIR/supervisor.lock`. SIGTERM: stop the pool
and the runner; shims and payloads stay.
"""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
