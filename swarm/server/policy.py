"""Pure command allow-list policy and minimal payload environment."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from swarm.wire import EffectClass


@dataclass(frozen=True)
class Decision:
    """Approved command class and complete payload environment."""
    allowed: bool
    effect_class: EffectClass = "external"
    reason: str = ""
    env: dict[str, str] = field(default_factory=dict)


class CommandPolicy(Protocol):
    def check(self, argv: list[str], private_dir: Path) -> Decision:
        """Decide for one command without I/O."""
        ...


@dataclass(frozen=True)
class AllowList:
    """Allow bare program names and supply a minimal environment."""
    programs: dict[str, EffectClass]
    path: str = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

    def check(self, argv: list[str], private_dir: Path) -> Decision:
        """Return a decision for a checked command."""
        program = argv[0] if argv else ""
        if not _bare_name(program) or program not in self.programs:
            return Decision(False, reason="program is not allowed")
        return Decision(True, self.programs[program], env={
            "PATH": self.path, "LANG": "C.UTF-8", "HOME": str(private_dir / "home"),
            "TMPDIR": str(private_dir / "tmp"),
        })


def default_policy() -> AllowList:
    """Return the default allow-list; all programs are external."""
    return AllowList({name: "external" for name in (
        "python3", "sh", "ls", "cat", "grep", "git", "make", "pytest", "sleep", "echo", "curl")})


def load_policy(path: Path) -> AllowList:
    """Load and validate a JSON program allow-list."""
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) != {"programs"} or not isinstance(data["programs"], dict):
        raise ValueError("policy must contain a programs object")
    programs = data["programs"]
    if any(not _bare_name(name) or kind not in ("local", "external") for name, kind in programs.items()):
        raise ValueError("invalid policy program or effect class")
    return AllowList(programs)


def _bare_name(program: str) -> bool:
    return bool(program) and "/" not in program and program not in (".", "..")
