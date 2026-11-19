"""Subprocess entry point for a shim with a stand-in launch client."""

import sys
import time
from pathlib import Path

from swarm import wire
from swarm.client import Unreachable
from swarm.supervisor.shim import run_shim


class StandIn:
    def __init__(self, directory: Path, mode: str) -> None:
        self.directory = directory
        self.mode = mode

    def grant(self, pid: int, *, deadline: float) -> wire.GrantReply:
        (self.directory / "pid").write_text(str(pid))
        assert deadline > time.monotonic()
        assert not (self.directory / "ran").exists()
        return wire.GrantReply(granted=self.mode != "deny")

    def heartbeat(self, log_bytes: int, truncated_bytes: int,
                  timeout_s: float = 0.5) -> wire.LaunchHeartbeatReply:
        assert timeout_s == 0.5
        with (self.directory / "heartbeats").open("a") as file:
            file.write(f"{log_bytes},{truncated_bytes}\n")
        if self.mode == "outage":
            time.sleep(timeout_s)
            raise Unreachable()
        return wire.LaunchHeartbeatReply(stop=self.mode == "stop", grace_s=0.3)

    def report_exit(self, report: wire.ExitReport, *, deadline: float) -> wire.Ack:
        assert deadline > time.monotonic()
        if report.started:
            assert wire.ExitReport.model_validate_json(
                (self.directory / "exit.json").read_bytes(),
            ) == report
        (self.directory / "reported.json").write_text(report.model_dump_json())
        return wire.Ack()


if __name__ == "__main__":
    order = wire.LaunchOrder.model_validate_json(sys.stdin.buffer.read())
    directory = Path(order.log_path).parent
    raise SystemExit(run_shim(order, StandIn(directory, sys.argv[1]), str(directory)))
