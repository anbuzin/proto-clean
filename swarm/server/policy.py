"""Command policy: which commands an agent can run, with what environment, and their class.

The server asks the policy (a pure function) before it records a process, and stores the
approved argv and environment on the row. A command with no file or network confinement
can have effects outside its sandbox copy, so every program is `external` unless the
owner's policy file marks it `local` (see "Limits" in ARCHITECTURE.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from swarm.wire import EffectClass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    effect_class: EffectClass = "external"   # a cut `external` command blocks the agent (I16)
    reason: str = ""
    env: dict[str, str] = field(default_factory=dict)  # the full payload env; no token, no key


class CommandPolicy(Protocol):
    def check(self, argv: list[str], private_dir: Path) -> Decision:
        """Decide for one command. Pure: no I/O. `private_dir` holds the payload's
        HOME (`home/`) and TMPDIR (`tmp/`); the shim makes them."""
        ...


@dataclass(frozen=True)
class AllowList:
    """Allow `argv[0]` if it is a bare program name in `programs`; the value is its class.
    Env: `PATH`, `LANG=C.UTF-8`, `HOME`, `TMPDIR`."""
    programs: dict[str, EffectClass]
    path: str = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

    def check(self, argv: list[str], private_dir: Path) -> Decision:
        raise NotImplementedError


def default_policy() -> AllowList:
    """`python3`, `sh`, `ls`, `cat`, `grep`, `git`, `make`, `pytest`, `sleep`, `echo`, `curl`,
    all `external`. Everything else is refused."""
    raise NotImplementedError


def load_policy(path: Path) -> AllowList:
    """`{"programs": {"name": "local" | "external"}}` from a JSON file (`--policy`)."""
    raise NotImplementedError
