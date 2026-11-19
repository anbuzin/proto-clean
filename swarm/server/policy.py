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
        if not argv or not isinstance(argv[0], str) or not argv[0] or "/" in argv[0] or argv[0] in (".", "..") or argv[0] not in self.programs:
            return Decision(False, reason="program is not allowed")
        return Decision(True, self.programs[argv[0]], env={
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
    if any(not isinstance(k, str) or not k or "/" in k or k in (".", "..") or v not in ("local", "external") for k, v in programs.items()):
        raise ValueError("invalid policy program or effect class")
    return AllowList(programs)
