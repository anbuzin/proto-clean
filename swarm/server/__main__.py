"""Entry point: `python -m swarm.server --state DIR [--host 127.0.0.1] [--port N] [--policy FILE]`.

Port rule (stable endpoint): `--port N` is used and saved in `DIR/server.json`; with no
`--port`, the port saved in `server.json` is used again, else 8700. `--port 0` picks a
free port once (tests) and saves it, so a restart on the same state directory listens
on the same URL and every running worker, runner, and shim reaches it again.
"""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, build `ServerConfig`, run `app.Server` until SIGTERM or SIGINT."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
