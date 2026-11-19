"""Entry point: `python -m swarm.worker --server URL [--kinds loop,external] [--tools MODULE:ATTR ...]`.

At start the worker reads `SWARM_WORKER_TOKEN` and `AI_GATEWAY_API_KEY` and removes both
from `os.environ`, so no child (tool, Fx CLI) inherits them. The model factory is given
at construction: `main(model_factory=...)`; the product passes `models.gateway_model`.
A test runs its own entry script that calls `main(model_factory=fake)`. `--tools` adds
custom tool entries (a product extension point).
"""

from __future__ import annotations

from swarm.client import AgentClient
from swarm.wire import AgentKind

from .models import ModelFactory
from .tools import Registry


async def run_worker(client: AgentClient, worker_id: str, kinds: list[AgentKind],
                     model_factory: ModelFactory, registry: Registry) -> None:
    """Forever: claim (long poll); build a `Lease`; enter `model_factory(claim.spec)`;
    run `loop.run_loop_agent` or `external.run_external_agent`; close both. `Rejected(stale)`
    ends the claim, not the worker; a stale reply to a claim retry means its reserved
    attempt ended: the next claim uses a new request ID. Any other error: exit code 1 (the pool starts a new one)."""
    raise NotImplementedError


def main(argv: list[str] | None = None, *, model_factory: ModelFactory | None = None) -> int:
    """`model_factory` None: `models.gateway_model` with the key read at start."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
