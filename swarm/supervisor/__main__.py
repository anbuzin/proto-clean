"""Entry point: `python -m swarm.supervisor --state DIR [--workers N] [--no-runner]
[-- WORKER_ARGS...]`.

Reads `server.json` and `tokens.json` from DIR. Runs a `WorkerPool` of N workers
(`python -m swarm.worker --server URL WORKER_ARGS`, token in `SWARM_WORKER_TOKEN`) and,
unless `--no-runner`, a `Runner` thread. Holds `DIR/supervisor.lock` (one per state
directory). SIGTERM: stop the pool and the runner; shims and their payloads stay.
"""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
