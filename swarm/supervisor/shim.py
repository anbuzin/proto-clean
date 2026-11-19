"""Shim: the parent of one payload. One thread; standard library and `swarm.client` only.

Entry: `python -m swarm.supervisor.shim --server URL --launch LAUNCH_ID`; the
`LaunchOrder` JSON arrives on stdin. Steps (I12, I13):

1. Read stdin to EOF; reopen fd 0 on /dev/null. Make `private_dirs` (0700).
2. Two pipes: barrier, output. Fork. Child: `setpgid(0, 0)`; keep only the barrier
   reader and the output writer (as fds 1 and 2); read one byte. Not `b"G"`: `_exit(71)`.
   Else `chdir(cwd)`, `execve` of the approved argv with the order's env; error: `_exit(127)`.
3. `grant(pid)` until the grant deadline. Not granted: kill the held child (unreaped,
   so its pid is safe), reap, `report_exit(started=False)`, exit.
4. Granted: write `b"G"`; close the barrier writer.
5. One loop on monotonic timers, `select` on the output pipe with a 0.2 s timeout:
   copy output to the log up to `log_cap_bytes` (count the rest as truncated);
   `waitpid(WNOHANG)`; each 1 s one `launch_heartbeat` try (0.5 s timeout; a failure
   is ignored). `stop` (from a reply, kept once seen): `killpg(TERM)`, then `killpg(KILL)`
   after `grace_s`, by its own clock - only while the payload leader is not reaped.
6. After the exit: drain for at most 1 s; write `exit.json` (temp, fsync, rename);
   `report_exit` until it is acknowledged or 10 minutes pass; exit. The server imports
   `exit.json` if the report never arrives.
"""

from __future__ import annotations

from swarm import wire
from swarm.client import LaunchClient


def run_shim(order: wire.LaunchOrder, client: LaunchClient, process_dir: str) -> int:
    """Steps 1 to 6 after stdin is read. Returns 0."""
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
