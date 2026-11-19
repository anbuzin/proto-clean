"""Server assembly, startup recovery, and reaper lifecycle."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import replace
from pathlib import Path

from swarm import wire
from . import leases, processes
from .config import DEFAULT_PORT, ServerConfig
from .http import Deps, make_http_server
from .policy import default_policy, load_policy
from .store import Store


class Server:
    def __init__(self, config: ServerConfig) -> None:
        self.config = replace(config, state_dir=config.state_dir.resolve())
        self.deps: Deps
        self.url: str
        self._lock = None
        self._http = None
        self._http_thread = None
        self._reaper = None
        self._stop = threading.Event()

    def start(self) -> None:
        """Recover state and start HTTP and reaper threads before returning."""
        self.config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = (self.config.state_dir / "server.lock").open("a+")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            token_path = self.config.tokens_path
            if not token_path.exists():
                tokens = {name: secrets.token_urlsafe(32) for name in ("owner", "worker", "runner")}
                temporary = token_path.with_suffix(".json.tmp")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as file:
                    json.dump(tokens, file)
                temporary.replace(token_path)
            tokens = json.loads(token_path.read_text())
            if set(tokens) != {"owner", "worker", "runner"} or any(not isinstance(v, str) or not v for v in tokens.values()):
                raise ValueError("invalid tokens.json")
            port = self.config.port
            server_path = self.config.state_dir / "server.json"
            if port is None:
                port = json.loads(server_path.read_text())["port"] if server_path.exists() else DEFAULT_PORT
            self.config = replace(self.config, port=port)
            store = Store(self.config.db_path)
            store.initialize()
            policy = load_policy(self.config.policy_path) if self.config.policy_path else default_policy()
            self.deps = Deps(store, self.config, policy, tokens, time.time())
            leases.recover_after_restart(self.deps)
            processes.recover_after_restart(self.deps)
            self._stop.clear()
            self._reaper = threading.Thread(target=reaper_loop, args=(self.deps, self._stop), daemon=True)
            self._reaper.start()
            self._http = make_http_server(self.deps)
            actual_port = self._http.server_address[1]
            self.url = f"http://{self.config.host}:{actual_port}"
            self._http_thread = threading.Thread(target=self._http.serve_forever, daemon=True)
            self._http_thread.start()
            temporary = server_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps({"url": self.url, "port": actual_port, "pid": os.getpid()}))
            temporary.replace(server_path)
        except BaseException:
            self.stop()
            raise

    def serve_forever(self) -> None:
        """Wait for the server to stop."""
        self._stop.wait()

    def stop(self) -> None:
        """Stop the HTTP server and reaper, then release the lock."""
        self._stop.set()
        if hasattr(self, "deps"):
            self.deps.store.close()
        if self._http is not None:
            if self._http_thread is not None:
                self._http.shutdown()
                self._http_thread.join()
                self._http_thread = None
            self._http.server_close()
            self._http = None
        if self._reaper is not None:
            self._reaper.join()
            self._reaper = None
        if self._lock is not None:
            self._lock.close()
            self._lock = None


def health(deps: Deps, request: None) -> wire.Health:
    """Return server identity and startup time."""
    return wire.Health(ok=True, pid=os.getpid(), started_at=deps.started_at)


def reaper_loop(deps: Deps, stop: threading.Event) -> None:
    """Expire leases and launches and enforce lifetimes at each interval."""
    while not stop.wait(deps.config.reaper_interval_s):
        for reap in (leases.expire_leases, processes.expire_launches, processes.enforce_lifetimes):
            try:
                reap(deps)
            except Exception:
                logging.exception("reaper %s failed", reap.__name__)
