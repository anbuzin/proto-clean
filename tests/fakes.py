"""A scripted model factory and two tool extensions for subprocess tests."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import ai
from ai.testing import FakeModel
from ai.types.messages import ToolCallPart

from swarm import wire
from swarm.worker.__main__ import main as worker_main
from swarm.worker.tools import ToolEntry


def call(name: str, call_id: str, **args: wire.JSON) -> dict[str, wire.JSON]:
    """Describe one tool call with an ID that stays fixed across restarts."""
    return {"tool_name": name, "tool_call_id": call_id, "tool_args": json.dumps(args)}


@ai.tool
async def wait_for_release(path: str) -> str:
    """Wait for a test to create the release file."""
    while not Path(path).exists():
        await asyncio.sleep(0.05)
    return "released"


@ai.tool
async def unsafe_wait_for_release(path: str) -> str:
    """Wait for a release on a call that must not retry after a worker loss."""
    while not Path(path).exists():
        await asyncio.sleep(0.05)
    return "released"


TOOLS = [ToolEntry(wait_for_release, "idempotent"), ToolEntry(unsafe_wait_for_release, "unsafe")]


def main() -> int:
    """Start the normal worker with an offline model factory."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--script", type=Path)
    options, worker_args = parser.parse_known_args()

    @asynccontextmanager
    async def model(spec: wire.TaskSpec) -> AsyncIterator[ai.Model]:
        if options.script is None:
            yield FakeModel([ai.user_message("Say hello."), ai.assistant_message("Hello from the offline model.")])
            return
        responses = json.loads(options.script.read_text())[spec.prompt]
        messages = [ai.user_message(spec.prompt)]
        for response in responses:
            messages.append(ai.assistant_message(response) if isinstance(response, str) else
                            ai.assistant_message(*(ToolCallPart(**part) for part in response)))
        yield FakeModel(messages)

    return worker_main(worker_args, model_factory=model)


if __name__ == "__main__":
    raise SystemExit(main())
