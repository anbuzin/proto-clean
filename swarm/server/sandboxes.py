"""Sandboxes: one directory per agent, a new copy per attempt (I8).

Path: `state_dir/sandboxes/<agent_id>/a<attempt>/`. The source of a new copy is the
latest `ready` copy of the agent; for the first attempt it is `spec.workspace` (root),
the parent's latest ready copy (child), or nothing (an empty directory).
Two phases, like every file operation of the server: reserve in a transaction, clone
with no transaction open, commit `ready`. An old writer, even an escaped process,
writes only to its own old copy, which no later attempt reads.
"""

from __future__ import annotations

from pathlib import Path

from .store import Store, Tx


def reserve(tx: Tx, sandboxes_dir: Path, agent_id: str, attempt: int) -> tuple[Path, Path | None]:
    """Return (new path, source path or None) and store the path on the attempt
    (`sandbox_state='cloning'`). Never reuses a path."""
    raise NotImplementedError


def materialize(source: Path | None, target: Path) -> None:
    """Clone `source` to `target` (`clone_tree`), or make an empty directory.
    Idempotent: a partial `target` from a crash is removed first."""
    raise NotImplementedError


def mark_ready(tx: Tx, agent_id: str, attempt: int) -> None:
    raise NotImplementedError


def clone_tree(source: Path, target: Path) -> None:
    """APFS `clonefile(2)` through ctypes on macOS (fast, copy on write); else
    `shutil.copytree(symlinks=True)`. Refuses special files."""
    raise NotImplementedError


def recover(store: Store) -> None:
    """Startup: each attempt with `sandbox_state='cloning'` is closed (its attempt is
    already ended by `recover_after_restart`); its partial directory stays unused."""
    raise NotImplementedError
