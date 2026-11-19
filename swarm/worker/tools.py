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
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import ai
from ai.types.tools import Tool, ToolSpec

from swarm import wire
from swarm.wire import Safety


@dataclass(frozen=True)
class CallContext:
    sandbox: Path
    agent_id: str
    attempt: int
    step_seq: int
    call_id: str


_current: contextvars.ContextVar[CallContext] = contextvars.ContextVar("swarm_call")


def current_call() -> CallContext:
    """Set by the loop before `ToolRunner.schedule`; the scheduled task copies the context."""
    return _current.get()


@dataclass(frozen=True)
class ToolEntry:
    tool: ai.AgentTool
    safety: Safety


class Registry:
    def __init__(self, entries: list[ToolEntry]) -> None:
        self._entries = {entry.tool.name: entry for entry in entries}
        if len(self._entries) != len(entries):
            raise ValueError("Duplicate tool name")

    def get(self, name: str) -> ToolEntry | None:
        """Find the registered tool and its safety class."""
        return self._entries.get(name)

    def entries(self, names: list[str]) -> list[ToolEntry]:
        """The entries for the names of a spec that the registry has, in order."""
        return [self._entries[name] for name in dict.fromkeys(names) if name in self._entries]

    def agent(self, names: list[str]) -> ai.Agent:
        """`ai.Agent(tools=...)` for the names of a spec that the registry has; used for
        `resolve` and tool schemas. Unknown names are left out (their calls get `error`)."""
        return ai.Agent(tools=[entry.tool for entry in self.entries(names)])


def _components(path: str) -> tuple[str, ...]:
    parsed = Path(path)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError("Expected a sandbox-relative path with no '..'")
    return parsed.parts


@contextmanager
def _directory(parts: tuple[str, ...]) -> Iterator[int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(current_call().sandbox, flags)
    try:
        for part in parts:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _file_path(path: str) -> tuple[tuple[str, ...], str]:
    parts = _components(path)
    if not parts:
        raise ValueError("Expected a file path")
    return parts[:-1], parts[-1]


@ai.tool
async def read_file(path: str) -> str:
    """Read a UTF-8 text file in the sandbox."""
    parents, name = _file_path(path)
    with _directory(parents) as directory:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(name, flags, dir_fd=directory), encoding="utf-8") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ValueError("Expected a regular file")
            return file.read()


@ai.tool
async def write_file(path: str, content: str) -> str:
    """Write a UTF-8 text file in the sandbox. Safe to run again."""
    parents, name = _file_path(path)
    with _directory(parents) as directory:
        try:
            mode = os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(mode):
                raise ValueError("Expected a regular file")
        temporary = f".swarm-{uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(temporary, flags, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            # I4: make the new name durable before returning the result to the loop.
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
    return path


@ai.tool
async def list_files(path: str = "") -> list[str]:
    """List files below a directory of the sandbox."""
    with _directory(_components(path)) as directory:
        with os.scandir(directory) as listing:
            return sorted(entry.name for entry in listing if not entry.is_symlink())


@ai.tool
async def _server_only() -> None:
    raise RuntimeError("Server tools cannot run in the worker")


def server_stubs() -> list[ToolEntry]:
    """One stub per name in `wire.SERVER_TOOLS`, safety `server`, with the text that the
    model sees (run_child: "Start a child agent and wait for its result")."""
    descriptions = {
        "memory_set": "Set a string in this agent's persistent memory",
        "run_child": "Start a child agent and wait for its result",
        "run_command": "Run an approved command in this sandbox and wait for its result",
        "process_start": "Start an approved background process in its own sandbox copy",
        "process_read": "Read a chunk of the background process log",
        "process_stop": "Stop a background process",
    }
    entries = []
    for name, args in wire.SERVER_TOOL_ARGS.items():
        spec = ToolSpec(description=descriptions[name], params=args.model_json_schema())
        declaration = Tool(kind="function", name=name, spec=spec)
        entries.append(ToolEntry(replace(_server_only, tool=declaration, validator=args), "server"))
    return entries


def builtin_registry() -> Registry:
    """The three file tools (`idempotent`) and the server stubs."""
    return Registry(_builtin_entries())


def _builtin_entries() -> list[ToolEntry]:
    return [ToolEntry(tool, "idempotent") for tool in (read_file, write_file, list_files)] + server_stubs()


def load_entries(ref: str) -> list[ToolEntry]:
    """Import `module:attr`, a list of `ToolEntry`, for `--tools`."""
    module, separator, attr = ref.partition(":")
    if not separator or not module or not attr:
        raise ValueError("--tools needs module:attr")
    entries = getattr(import_module(module), attr)
    if not isinstance(entries, list) or any(
        not isinstance(entry, ToolEntry) or entry.safety not in ("idempotent", "unsafe") for entry in entries
    ):
        raise ValueError("--tools needs a list of local ToolEntry values")
    return entries
