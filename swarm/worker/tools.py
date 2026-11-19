"""The tool registry: local tools, server tool stubs, and their safety class.

A local tool runs in the worker inside the current attempt's sandbox copy and gets its
context from `current_call()`, never from model arguments. A server tool is an `@ai.tool`
stub with the arguments of its `wire.SERVER_TOOL_ARGS` model; the loop uses it only for
the schema and never calls its body (the server runs it).

File tools refuse absolute paths, `..`, and symlinks: each path component is opened
with `O_NOFOLLOW` relative to the sandbox directory descriptor. `write_file` writes a
temp file, `fsync`s it, renames it, and `fsync`s the directory before it returns; its
result is acknowledged only after that (I4).
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from pathlib import Path

import ai

from swarm.wire import Safety


@dataclass(frozen=True)
class CallContext:
    sandbox: Path               # this attempt's copy
    agent_id: str
    attempt: int
    step_seq: int
    call_id: str


_current: contextvars.ContextVar[CallContext] = contextvars.ContextVar("swarm_call")


def current_call() -> CallContext:
    """Set by the loop before `ToolRunner.schedule`; the scheduled task copies the context."""
    raise NotImplementedError


@dataclass(frozen=True)
class ToolEntry:
    tool: ai.AgentTool          # what `@ai.tool` returns; `.tool` is the declaration for `ai.stream`
    safety: Safety


class Registry:
    def __init__(self, entries: list[ToolEntry]) -> None:
        raise NotImplementedError

    def get(self, name: str) -> ToolEntry | None:
        raise NotImplementedError

    def entries(self, names: list[str]) -> list[ToolEntry]:
        """The entries for the names of a spec that the registry has, in order."""
        raise NotImplementedError

    def agent(self, names: list[str]) -> ai.Agent:
        """`ai.Agent(tools=...)` for the names of a spec that the registry has; used for
        `resolve` and tool schemas. Unknown names are left out (their calls get `error`)."""
        raise NotImplementedError


@ai.tool
async def read_file(path: str) -> str:
    """Read a UTF-8 text file in the sandbox."""
    raise NotImplementedError


@ai.tool
async def write_file(path: str, content: str) -> str:
    """Write a UTF-8 text file in the sandbox. Safe to run again."""
    raise NotImplementedError


@ai.tool
async def list_files(path: str = "") -> list[str]:
    """List files below a directory of the sandbox."""
    raise NotImplementedError


def server_stubs() -> list[ToolEntry]:
    """One stub per name in `wire.SERVER_TOOLS`, safety `server`, with the text that the
    model sees (run_child: "Start a child agent and wait for its result")."""
    raise NotImplementedError


def builtin_registry() -> Registry:
    """The three file tools (`idempotent`) and the server stubs."""
    raise NotImplementedError


def load_entries(ref: str) -> list[ToolEntry]:
    """Import `module:attr`, a list of `ToolEntry`, for `--tools`."""
    raise NotImplementedError
