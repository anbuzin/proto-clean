"""Dependencies shared by route handlers."""

from __future__ import annotations

from dataclasses import dataclass

from .config import ServerConfig
from .policy import CommandPolicy
from .store import Store


@dataclass(frozen=True)
class Deps:
    """Shared server state plus the per-request launch secret, when present."""
    store: Store
    config: ServerConfig
    policy: CommandPolicy
    tokens: dict[str, str]
    started_at: float
    launch_secret: str | None = None
