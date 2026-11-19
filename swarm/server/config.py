"""Server settings. One frozen dataclass; no global state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .policy import CommandPolicy


@dataclass(frozen=True)
class ServerConfig:
    state_dir: Path                     # holds state.db, tokens.json, server.lock, sandboxes/, processes/
    host: str = "127.0.0.1"
    port: int = 0
    lease_ttl_s: float = 15.0
    reaper_interval_s: float = 0.5
    max_strikes: int = 3
    backoff_base_s: float = 1.0         # backoff = min(base * 2**(strikes-1), backoff_max_s)
    backoff_max_s: float = 60.0
    grant_deadline_s: float = 10.0      # an issued launch that is not granted by then is revoked
    launch_tries: int = 3
    shim_silence_s: float = 10.0        # no shim heartbeat for this long: outcome `unknown`
    log_cap_bytes: int = 64 << 20
    policy: CommandPolicy | None = None # None = policy.default_policy()

    @property
    def db_path(self) -> Path:
        return self.state_dir / "state.db"

    @property
    def sandboxes_dir(self) -> Path:
        return self.state_dir / "sandboxes"

    @property
    def processes_dir(self) -> Path:
        return self.state_dir / "processes"

    @property
    def tokens_path(self) -> Path:
        return self.state_dir / "tokens.json"
