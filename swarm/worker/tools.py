"""The tool registry: local tools, server tool stubs, and their safety class.

A local tool runs in the worker, inside the sandbox copy of the current attempt. It gets
its context from `current_call()`, never from model arguments. A server tool is an
`@ai.tool` stub with the same arguments as its `wire.SERVER_TOOL_ARGS` model; the loop
uses it only for the schema and never calls its body (the server runs it).

Safety: `server` (the server does it, exactly once), `idempotent` (safe to run again
with the same call key), `unsafe` (an interrupted run becomes `unknown` for the owner).
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ai

from swarm.wire import Safety


@dataclass(frozen=True)
class CallContext:
    """What a running local tool may use."""
    sandbox: Path                         # this attempt's copy; all paths resolve below it
    agent_id: str
    attempt: int
    step_seq: int
    call_id: str
    checkpoint: dict[str, Any] | None     # saved by an earlier run of the same call
    save_checkpoint: Any                  # Callable[[dict], None]: fenced write to the server


_current: contextvars.ContextVar[CallContext] = contextvars.ContextVar("swarm_call")


def current_call() -> CallContext:
    """The context of the running call. Set by the loop before `ToolRunner.schedule`."""
    raise NotImplementedError


@dataclass(frozen=True)
class ToolEntry:
    tool: ai.Tool
    safety: Safety


class Registry:
    def __init__(self, entries: list[ToolEntry]) -> None:
        raise NotImplementedError

    def get(self, name: str) -> ToolEntry:
        raise NotImplementedError

    def for_spec(self, names: list[str]) -> list[ToolEntry]:
        """The entries that a task spec names, in order. Unknown name: `KeyError`."""
        raise NotImplementedError

    def agent(self, names: list[str]) -> ai.Agent:
        """`ai.Agent(tools=...)`, used for `resolve` and for tool schemas."""
        raise NotImplementedError


def resolve_in_sandbox(path: str) -> Path:
    """A relative path below `current_call().sandbox`; refuses `..`, absolute paths, and
    symlinks that leave the sandbox."""
    raise NotImplementedError


# Local tools (bodies are part of the build): read_file, write_file, list_files.

@ai.tool
async def read_file(path: str) -> str:
    """Read a UTF-8 text file in the sandbox."""
    raise NotImplementedError


@ai.tool
async def write_file(path: str, content: str) -> str:
    """Write a UTF-8 text file in the sandbox (temp file, then rename). Idempotent."""
    raise NotImplementedError


@ai.tool
async def list_files(path: str = "") -> list[str]:
    """List files below a directory of the sandbox."""
    raise NotImplementedError


def server_stubs() -> list[ToolEntry]:
    """One stub per name in `wire.SERVER_TOOLS`, safety `server`, with the docstring that
    the model sees (for example: run_child "Start a child agent and wait for its result")."""
    raise NotImplementedError


def builtin_registry() -> Registry:
    """read_file (idempotent), write_file (idempotent), list_files (idempotent), and the
    server stubs."""
    raise NotImplementedError


def load_entries(ref: str) -> list[ToolEntry]:
    """Import `module:attr`, a list of `ToolEntry`, for `--tools`."""
    raise NotImplementedError
