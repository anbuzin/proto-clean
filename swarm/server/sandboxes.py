"""Sandbox copies: one directory per attempt (I8).

Path: `state_dir/sandboxes/<agent_id>/a<attempt>/`. Source of a new copy: the agent's
latest `ready` copy. If the agent has no ready copy yet (whatever the attempt number,
so a failed first clone does not change the source): `spec.workspace` (root), the
pinned seed attempt's copy (child), or nothing (empty). The source is stable in the normal crash: its attempt
has ended (the worker is dead or yielded), `run_command` ended before the agent became
ready, and `process_start` payloads run in their own clone, not in the agent's copy.
"""

from __future__ import annotations

from pathlib import Path

from .store import Tx


def reserve(tx: Tx, sandboxes_dir: Path, agent: dict, attempt: int) -> tuple[Path, Path | None]:
    """(new path, source or None); stores the path on the attempt, `cloning`. Never reuses a path."""
    raise NotImplementedError


def materialize(source: Path | None, target: Path) -> None:
    """Clone `source` to `target`, or make an empty directory. A partial `target` from an
    earlier try is removed first. Raises `OSError` on failure (the claim handles it)."""
    raise NotImplementedError


def mark_ready(tx: Tx, agent_id: str, attempt: int) -> None:
    raise NotImplementedError


def clone_tree(source: Path, target: Path) -> None:
    """Regular files and directories only: refuse symlinks, devices, sockets, FIFOs, and
    files with more than one hard link (`OSError`). APFS `clonefile(2)` per file through
    ctypes on macOS; `shutil.copy2` elsewhere."""
    raise NotImplementedError
