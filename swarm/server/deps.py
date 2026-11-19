"""Dependencies shared by route handlers."""

from dataclasses import dataclass

from .config import ServerConfig
from .policy import CommandPolicy
from .store import Store


@dataclass(frozen=True)
class Deps:
    """Dependencies shared by route handlers."""
    store: Store
    config: ServerConfig
    policy: CommandPolicy
    tokens: dict[str, str]
    started_at: float
    launch_secret: str | None = None
