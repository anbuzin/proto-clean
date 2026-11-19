"""Sandbox copies: one directory per attempt (I8).

Path: `state_dir/sandboxes/<agent_id>/a<attempt>/`. Source of a new copy: the agent's
latest `ready` copy. If the agent has no ready copy yet (whatever the attempt number,
so a failed first clone does not change the source): `spec.workspace` (root), the
pinned seed attempt's copy (child), or nothing (empty). The source is stable in the normal crash: its attempt
has ended (the worker is dead or yielded), `run_command` ended before the agent became
ready, and `process_start` payloads run in their own clone, not in the agent's copy.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import shutil
import stat
import sys
from pathlib import Path

from .store import Tx


def reserve(tx: Tx, sandboxes_dir: Path, agent: dict, attempt: int) -> tuple[Path, Path | None]:
    """(new path, source or None); stores the path on the attempt, `cloning`. Never reuses a path."""
    target = sandboxes_dir / agent["id"] / f"a{attempt}"
    source = tx.one(
        "SELECT sandbox_path FROM attempts WHERE agent_id=? AND attempt<? AND sandbox_state='ready' "
        "AND ended_at IS NOT NULL ORDER BY attempt DESC LIMIT 1", agent["id"], attempt,
    )
    if source is None and agent["seed_agent_id"]:
        source = tx.require("SELECT sandbox_path FROM attempts WHERE agent_id=? AND attempt=?",
                            agent["seed_agent_id"], agent["seed_attempt"])
    workspace = source["sandbox_path"] if source else json.loads(agent["spec_json"])["workspace"]
    tx.run("UPDATE attempts SET sandbox_path=?, sandbox_state='cloning' WHERE agent_id=? AND attempt=?",
           str(target), agent["id"], attempt)
    return target, Path(workspace) if workspace else None


def materialize(source: Path | None, target: Path) -> None:
    """Clone `source` to `target`, or make an empty directory. A partial `target` from an
    earlier try is removed first. Raises `OSError` on failure (the claim handles it)."""
    if source is not None and target.resolve().is_relative_to(source.resolve()):
        raise OSError(errno.EINVAL, "copy destination is inside source", str(target))
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source is None:
        target.mkdir(mode=0o700)
    else:
        clone_tree(source, target)


def mark_ready(tx: Tx, agent_id: str, attempt: int) -> None:
    """Publish the completed sandbox copy inside the claim transaction."""
    tx.run("UPDATE attempts SET sandbox_state='ready' WHERE agent_id=? AND attempt=?", agent_id, attempt)


def clone_tree(source: Path, target: Path) -> None:
    """Regular files and directories only: refuse symlinks, devices, sockets, FIFOs, and
    files with more than one hard link (`OSError`). APFS `clonefile(2)` per file through
    ctypes on macOS; `shutil.copy2` elsewhere."""
    if not stat.S_ISDIR(source.lstat().st_mode):
        raise OSError(errno.EINVAL, "sandbox source must be a directory", str(source))
    target.mkdir(mode=0o700)
    for entry in source.iterdir():
        info = entry.lstat()
        copied = target / entry.name
        if stat.S_ISDIR(info.st_mode):
            clone_tree(entry, copied)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            _clone_file(entry, copied)
        else:
            raise OSError(errno.EINVAL, "unsupported sandbox entry", str(entry))


def _clone_file(source: Path, target: Path) -> None:
    if sys.platform == "darwin":
        clonefile = ctypes.CDLL(None, use_errno=True).clonefile
        clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
        clonefile.restype = ctypes.c_int
        if clonefile(os.fsencode(source), os.fsencode(target), 0) == 0:
            return
        error = ctypes.get_errno()
        if error not in {errno.EXDEV, errno.ENOTSUP, errno.ENOSYS}:
            raise OSError(error, os.strerror(error), str(source))
    shutil.copy2(source, target)
