"""Shim: the parent of one payload. One thread; standard library and `swarm.client` only.

Entry: `python -m swarm.supervisor.shim --server URL --launch LAUNCH_ID`; the
`LaunchOrder` JSON arrives on stdin. Steps, in this order (I12, I13):

1. Read stdin to EOF; reopen fd 0 on /dev/null. Make the log directory.
2. Two pipes: barrier and output. Fork. The child: `setpgid(0, 0)`, keep only the barrier
   reader and the output writer (as fds 1 and 2), read one byte from the barrier. Not
   `b"G"` (EOF: the shim died or was denied): `_exit(71)`. Else `chdir(cwd)` and
   `execvpe(argv, env)`; on failure `_exit(127)`.
3. `grant(pid)` with retries until the grant deadline. Not granted: kill the child
   (it is unreaped, so its pid is safe to signal), reap, send `report_exit(started=False)`, exit.
4. Granted: write `b"G"`, close the barrier writer.
5. Loop: `waitpid(WNOHANG)`; copy output to the log file up to `log_cap_bytes` (count
   the rest as truncated); every 1 s `launch_heartbeat`. Reply `stop`: TERM to the
   payload group, KILL after `grace_s`.
6. After the exit: drain output for at most 1 s; `report_exit` with retries for up to
   10 minutes; exit.

A dead shim leaves its payload running with no parent that knows its exit: the server
marks the outcome `unknown` after `shim_silence_s` (no exit code is made up).
"""

from __future__ import annotations

from swarm import wire
from swarm.client import LaunchClient


def run_shim(order: wire.LaunchOrder, client: LaunchClient) -> int:
    """Steps 1 to 6 after stdin is read. Returns the shim exit code (0)."""
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
