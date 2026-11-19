"""Entry point: `python -m swarm.worker --server URL [--kinds loop,external]
[--model-factory MODULE:FUNC] [--tools MODULE:ATTR ...] [--worker-id ID]`.

The worker token comes from the environment variable `SWARM_WORKER_TOKEN`, never argv.
`--model-factory` defaults to `swarm.worker.models:gateway_model`. `--tools` adds tool
entries to the registry (custom tools are a normal extension, not a test hook).
"""

from __future__ import annotations

from swarm.client import AgentClient
from swarm.wire import AgentKind

from .models import ModelFactory
from .tools import Registry


async def run_worker(client: AgentClient, worker_id: str, kinds: list[AgentKind],
                     model_factory: ModelFactory, registry: Registry) -> None:
    """Forever: claim (long poll); for each claim build a `Lease`, dispatch by
    `claim.spec.kind` to `loop.run_loop_agent` or `external.run_external_agent`, then
    close the lease. A `Rejected(stale)` ends the claim, not the worker. Any other error
    ends the process with exit code 1 (the supervisor starts a new one)."""
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
