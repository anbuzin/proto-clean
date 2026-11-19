"""Hold the payload behind a pipe until the server grants the launch (I12).

The shim alone signals and reaps the payload. It caps the log, saves `exit.json`
before reporting the exit (I13), and can retry the report across a server crash.
The launch order and secret arrive on stdin; the payload gets only its approved
argv, cwd, and environment.
"""

from __future__ import annotations

import argparse
import logging
import os
import select
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

from swarm import wire
from swarm.client import LaunchClient, Rejected, Unreachable

logger = logging.getLogger(__name__)


def _exec_held(order: wire.LaunchOrder, barrier: int, output: int) -> None:
    try:
        os.setpgid(0, 0)
        os.dup2(output, 1)
        os.dup2(output, 2)
        os.close(output)
        if os.read(barrier, 1) != b"G":
            os._exit(71)
        os.close(barrier)
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        os.chdir(order.cwd)
        os.execvpe(order.argv[0], order.argv, order.env)
    except (OSError, ValueError, IndexError):
        os._exit(127)


class _Payload:
    def __init__(self, order: wire.LaunchOrder) -> None:
        barrier_read, barrier_write = os.pipe()
        try:
            output_read, output_write = os.pipe()
        except OSError:
            os.close(barrier_read)
            os.close(barrier_write)
            raise
        try:
            self.pid = os.fork()
        except OSError:
            for fd in (barrier_read, barrier_write, output_read, output_write):
                os.close(fd)
            raise
        if self.pid == 0:
            os.close(barrier_write)
            os.close(output_read)
            _exec_held(order, barrier_read, output_write)
        os.close(barrier_read)
        os.close(output_write)
        self.barrier: int | None = barrier_write
        self.output: int | None = output_read
        self.returncode: int | None = None

    def release(self) -> None:
        assert self.barrier is not None
        try:
            os.write(self.barrier, b"G")
        except BrokenPipeError:
            pass  # Wait status still records the exit if the barrier reader died.
        os.close(self.barrier)
        self.barrier = None

    def read(self, timeout: float) -> bytes:
        readers = [] if self.output is None else [self.output]
        ready, _, _ = select.select(readers, [], [], timeout)
        if not ready:
            return b""
        data = os.read(ready[0], 65536)
        if not data:
            os.close(ready[0])
            self.output = None
        return data

    def poll(self) -> int | None:
        if self.returncode is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def signal_group(self, sig: signal.Signals) -> None:
        # L3: never signal a group after its leader was reaped.
        if self.returncode is None:
            try:
                os.killpg(self.pid, sig)
            except ProcessLookupError:
                pass

    def close(self) -> None:
        if self.returncode is None:
            if self.barrier is None:
                self.signal_group(signal.SIGKILL)
            else:
                # I12: before release, only the held child can exist.
                try:
                    os.kill(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            _, status = os.waitpid(self.pid, 0)
            self.returncode = os.waitstatus_to_exitcode(status)
        for fd in (self.barrier, self.output):
            if fd is not None:
                os.close(fd)


class _Log:
    def __init__(self, file: BinaryIO, cap: int) -> None:
        self.file = file
        self.cap = cap
        self.size = 0
        self.truncated = 0

    def append(self, data: bytes) -> None:
        kept = data[:max(0, self.cap - self.size)]
        if kept:
            self.file.write(kept)
            self.file.flush()
        self.size += len(kept)
        self.truncated += len(data) - len(kept)

    def report(self, returncode: int) -> wire.ExitReport:
        self.file.seek(max(0, self.size - 4096))
        return wire.ExitReport(
            exit_code=returncode if returncode >= 0 else None,
            signal=-returncode if returncode < 0 else None,
            log_bytes=self.size, truncated_bytes=self.truncated,
            output_tail=self.file.read().decode("utf-8", errors="replace"),
        )


def _follow(payload: _Payload, log: _Log, client: LaunchClient) -> wire.ExitReport:
    heartbeat_at = time.monotonic() + 1
    kill_at = None
    while (returncode := payload.poll()) is None:
        now = time.monotonic()
        if kill_at is not None and now >= kill_at:
            payload.signal_group(signal.SIGKILL)
            kill_at = float("inf")
        if now >= heartbeat_at:
            try:
                reply = client.heartbeat(log.size, log.truncated, timeout_s=0.5)
            except (Unreachable, Rejected):
                reply = None
            heartbeat_at = now + 1
            if reply is not None and reply.stop and kill_at is None:
                payload.signal_group(signal.SIGTERM)
                kill_at = time.monotonic() + reply.grace_s
        log.append(payload.read(0.2))
    drain_until = time.monotonic() + 1
    while payload.output is not None and (remaining := drain_until - time.monotonic()) > 0:
        log.append(payload.read(remaining))
    return log.report(returncode)


def _save_exit_report(process_dir: Path, report: wire.ExitReport) -> None:
    with tempfile.NamedTemporaryFile(dir=process_dir, delete=False) as file:
        path = Path(file.name)
        try:
            file.write(report.model_dump_json().encode())
            file.flush()
            os.fsync(file.fileno())
            path.replace(process_dir / "exit.json")
        finally:
            path.unlink(missing_ok=True)


def run_shim(order: wire.LaunchOrder, client: LaunchClient, process_dir: str) -> int:
    """Steps 1 to 6 after stdin is read. `process_dir` is the parent of `order.log_path`;
    save the final exit report to `process_dir/exit.json`."""
    deadline = time.monotonic() + order.grant_deadline_s
    with open(os.devnull, "rb") as null:
        os.dup2(null.fileno(), 0)
    for directory in order.private_dirs:
        Path(directory).mkdir(mode=0o700, parents=True, exist_ok=True)

    payload = _Payload(order)
    try:
        try:
            granted = client.grant(payload.pid, deadline=deadline).granted
        except Unreachable:
            # An already committed grant replays; an expired, ungranted launch cannot run.
            try:
                granted = client.grant(payload.pid, deadline=time.monotonic() + 1).granted
            except (Unreachable, Rejected):
                granted = False
        except Rejected:
            granted = False
        if granted:
            with open(order.log_path, "w+b") as file:
                payload.release()
                report = _follow(payload, _Log(file, order.log_cap_bytes), client)
            _save_exit_report(Path(process_dir), report)
        else:
            # I12: only the granted launch can write the shared exit report.
            report = wire.ExitReport(started=False)
    finally:
        payload.close()
    try:
        client.report_exit(report, deadline=time.monotonic() + 600)
    except (Unreachable, Rejected):
        logger.warning("Exit report not acknowledged for launch %s", order.launch_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Read one launch order from stdin and run its shim."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Run the payload of one launch.")
    parser.add_argument("--server", required=True)
    parser.add_argument("--launch", required=True)
    args = parser.parse_args(argv)
    try:
        order = wire.LaunchOrder.model_validate_json(sys.stdin.buffer.read())
    except ValueError:
        parser.error("invalid launch order")
    if order.launch_id != args.launch:
        parser.error("launch order does not match --launch")
    client = LaunchClient(args.server, order.launch_id, order.secret)
    return run_shim(order, client, str(Path(order.log_path).parent))


if __name__ == "__main__":
    raise SystemExit(main())
