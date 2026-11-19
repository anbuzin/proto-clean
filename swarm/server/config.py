"""Server settings. One frozen dataclass; no global state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 8700


@dataclass(frozen=True)
class ServerConfig:
    state_dir: Path                     # state.db, tokens.json, server.json, sandboxes/, processes/
    host: str = "127.0.0.1"
    port: int | None = None             # None: port in server.json if present, else DEFAULT_PORT
    policy_path: Path | None = None     # JSON for policy.load_policy; None: policy.default_policy()
    lease_ttl_s: float = 15.0
    clone_ttl_s: float = 120.0          # lease of a claim while its sandbox copy is made
    max_clone_tries: int = 3            # then the agent fails ('sandbox')
    reaper_interval_s: float = 0.5
    max_strikes: int = 3
    backoff_base_s: float = 1.0         # backoff = min(base * 2**(strikes-1), backoff_max_s)
    backoff_max_s: float = 60.0
    grant_deadline_s: float = 10.0
    launch_tries: int = 3
    shim_silence_s: float = 10.0
    log_cap_bytes: int = 64 << 20

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
