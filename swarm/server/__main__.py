"""Run the server until SIGINT or SIGTERM."""
from __future__ import annotations

import argparse
import signal
import threading
from pathlib import Path

from .app import Server
from .config import ServerConfig


def main(argv: list[str] | None = None) -> int:
    """Parse options, start the server, and stop it on a signal."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    parser.add_argument("--policy", type=Path)
    args = parser.parse_args(argv)
    server = Server(ServerConfig(state_dir=args.state, host=args.host, port=args.port, policy_path=args.policy))
    stopped = threading.Event()
    old_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, lambda signum, frame: stopped.set())
    try:
        server.start()
        stopped.wait()
    finally:
        server.stop()
        for sig, old in old_handlers.items():
            signal.signal(sig, old)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
