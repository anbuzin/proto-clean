"""Entry point: `python -m swarm.server --state DIR [--host 127.0.0.1] [--port 0]`.

Starts one server on one state directory. Writes `DIR/server.json` = {url, pid} after it
listens (port 0 picks a free port), so tests and the supervisor can find it.
"""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, build `ServerConfig`, run `app.Server` until SIGTERM or SIGINT."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
