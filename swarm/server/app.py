"""Server assembly: lock, tokens, store, startup recovery, reaper thread, HTTP.

Startup order (each step before the next):
1. Take an exclusive `fcntl` lock on `state_dir/server.lock`; a second server on the
   same state directory exits.
2. Create `tokens.json` (mode 0600) with random `owner`, `worker`, `runner` tokens, once.
3. `store.initialize()`.
4. `leases.recover_after_restart()`: end every open attempt as `server_restart` (no strike).
5. `sandboxes.recover()`: finish or drop `cloning` copies.
6. `processes.recover_after_restart()`: revoke `issued` launches.
7. Start the reaper thread, then listen, then write `server.json`.
"""

from __future__ import annotations

import threading

from .config import ServerConfig
from .policy import CommandPolicy
from .store import Store


class Server:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.policy: CommandPolicy
        self.store: Store
        self.tokens: dict[str, str]
        self.url: str
        raise NotImplementedError

    def start(self) -> None:
        """Run the startup order above. Returns when the server listens."""
        raise NotImplementedError

    def serve_forever(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        """Stop the reaper and HTTP threads; release the lock. Does not touch processes."""
        raise NotImplementedError


def reaper_loop(server: Server, stop: threading.Event) -> None:
    """Every `reaper_interval_s`: `leases.expire_leases`, `processes.expire_launches`,
    `processes.enforce_lifetimes`. Each in its own transaction; an error is logged, not fatal."""
    raise NotImplementedError
