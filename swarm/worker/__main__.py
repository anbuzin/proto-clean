"""Entry point: `python -m swarm.worker --server URL [--kinds loop,external] [--tools MODULE:ATTR ...]`.

At start the worker reads `SWARM_WORKER_TOKEN` and `AI_GATEWAY_API_KEY` and removes both
from `os.environ`, so no child (tool, Fx CLI) inherits them. The model factory is given
at construction: `main(model_factory=...)`; the product passes `models.gateway_model`.
A test runs its own entry script that calls `main(model_factory=fake)`. `--tools` adds
custom tool entries (a product extension point).
"""

import argparse
import asyncio
import os
import sys
from typing import cast
from uuid import uuid4

from swarm.client import AgentClient, Rejected
from swarm.wire import AgentKind

from . import external, lease, loop, models, tools


async def run_worker(client: AgentClient, worker_id: str, kinds: list[AgentKind],
                     model_factory: models.ModelFactory, registry: tools.Registry) -> None:
    """Forever: claim (long poll); build a `Lease`; enter `model_factory(claim.spec)`;
    run `loop.run_loop_agent` or `external.run_external_agent`; close both. `Rejected(stale)`
    ends the claim, not the worker; a stale reply to a claim retry means its reserved
    attempt ended: the next claim uses a new request ID. Any other error: exit code 1 (the pool starts a new one)."""
    while True:
        try:
            claim = client.claim(worker_id, os.getpid(), kinds)
            if claim is None:
                continue
            claim_lease = lease.Lease(client, claim)
            try:
                claim_lease.start()
                if claim.spec.kind == "external":
                    await external.run_external_agent(claim_lease, claim)
                else:
                    async with model_factory(claim.spec) as model:
                        await loop.run_loop_agent(claim_lease, claim, model, registry)
            finally:
                claim_lease.close()
        except Rejected as error:
            if not error.stale:
                raise


def main(argv: list[str] | None = None, *, model_factory: models.ModelFactory | None = None) -> int:
    """`model_factory` None: `models.gateway_model` with the key read at start."""
    token = os.environ.pop("SWARM_WORKER_TOKEN", None)
    key = os.environ.pop("AI_GATEWAY_API_KEY", "")
    parser = argparse.ArgumentParser(description="Run a swarm worker")
    parser.add_argument("--server", required=True)
    parser.add_argument("--kinds", default="loop,external")
    parser.add_argument("--tools", action="append", default=[], metavar="MODULE:ATTR")
    args = parser.parse_args(argv)
    kinds = args.kinds.split(",")
    if not token or not all(kind in ("loop", "external") for kind in kinds):
        print("Set SWARM_WORKER_TOKEN and use --kinds loop,external or a subset", file=sys.stderr)
        return 1
    try:
        entries = tools._builtin_entries() + [entry for ref in args.tools for entry in tools.load_entries(ref)]
        asyncio.run(run_worker(AgentClient(args.server, token), uuid4().hex, cast(list[AgentKind], kinds),
                               model_factory or models.gateway_model(key), tools.Registry(entries)))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"swarm worker: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
