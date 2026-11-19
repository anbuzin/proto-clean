"""Server assembly: lock, tokens, store, startup recovery, reaper thread, HTTP.

Startup order (each step before the next):
1. Exclusive `fcntl` lock on `state_dir/server.lock`; a second server exits.
2. `tokens.json` (mode 0600, keys `owner`, `worker`, `runner`), made once.
3. `store.initialize()`.
4. `leases.recover_after_restart()`: end every open attempt `server_restart` (no strike).
5. `processes.recover_after_restart()`: revoke `issued` launches; set heartbeat clocks.
6. Start the reaper thread; listen on the port (section "Endpoint" of ARCHITECTURE.md);
   write `server.json` = {url, port, pid}.
"""

from __future__ import annotations

import threading

from swarm import wire

from .config import ServerConfig
from .http import Deps


class Server:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.deps: Deps
        self.url: str
        raise NotImplementedError

    def start(self) -> None:
        """Run the startup order. Returns when the server listens."""
        raise NotImplementedError

    def serve_forever(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        """Stop the reaper and HTTP; release the lock. Does not touch processes."""
        raise NotImplementedError


def health(deps: Deps, request: None) -> wire.Health:
    raise NotImplementedError


def reaper_loop(deps: Deps, stop: threading.Event) -> None:
    """Every `reaper_interval_s`: `leases.expire_leases`, `processes.expire_launches`,
    `processes.enforce_lifetimes`. Each in its own transactions; an error is logged."""
    raise NotImplementedError
