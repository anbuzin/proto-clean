"""Command policy: which commands an agent can run, and how a cut run is treated.

The server asks the policy before it records a process. The policy is the one place
where "in the wild" rules live (allow lists, environment, effect class). A production
build replaces `AllowList` with a sandbox profile or a separate user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from swarm.wire import EffectClass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    effect_class: EffectClass = "local"   # `external`: a cut run blocks the agent for the owner (I16)
    reason: str = ""
    env: dict[str, str] = field(default_factory=dict)  # the full payload environment; no tokens


class CommandPolicy(Protocol):
    def check(self, argv: list[str], cwd: Path) -> Decision:
        """Decide for one command. Must be pure: no I/O, no side effects."""
        ...


@dataclass(frozen=True)
class AllowList:
    """Allow a command if `argv[0]` (a bare program name) is a key; the value is its class.
    The payload environment is `PATH`, `LANG`, and a private `HOME` and `TMPDIR` below cwd."""
    programs: dict[str, EffectClass]
    path: str = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

    def check(self, argv: list[str], cwd: Path) -> Decision:
        raise NotImplementedError


def default_policy() -> AllowList:
    """`python3`, `sh`, `ls`, `cat`, `grep`, `git`, `make`, `pytest`, `sleep`, `echo`: local.
    `curl`: external. Everything else is refused."""
    raise NotImplementedError


def load_policy(path: Path) -> AllowList:
    """Read `{"programs": {"name": "local"|"external"}}` from a JSON file."""
    raise NotImplementedError
