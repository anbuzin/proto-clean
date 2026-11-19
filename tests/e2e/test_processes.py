"""Run real payloads through the runner and launch shim."""

import base64
import json
import signal
from pathlib import Path

from swarm import wire
from tests.fakes import call
from tests.helpers import SwarmTest, matching_pids, send


PAYLOAD = """\
import sys, time
from pathlib import Path
print('first', flush=True)
print('second', flush=True)
deadline = time.monotonic() + 60
while not Path(sys.argv[1]).exists() and time.monotonic() < deadline:
    time.sleep(0.05)
sys.exit(7)
"""


class ProcessTest(SwarmTest):
    def command(self, release: Path, *, external: bool = False) -> list[str]:
        """Build a bounded payload that stays alive until its release file exists."""
        python = ["python3", "-c", PAYLOAD, str(release)]
        return ["sh", "-c", 'exec "$@"', "command", *python] if external else python

    def process(self, task: str, state: wire.ProcessState) -> wire.ProcessView:
        """Wait for the task's sole process to reach the given state."""
        records = self.until(lambda: self.owner.processes(task).processes,
                             lambda rows: len(rows) == 1 and rows[0].state == state)
        return records[0]

    def test_t11_command_yields_and_returns_exit_and_output(self) -> None:
        release = self.state / "release"
        self.set_scripts({"run command": [[
            call("run_command", "command", argv=self.command(release), timeout_s=45),
        ], "done"]})
        self.start_server()
        self.start_supervisor()
        task = self.submit("run command", "run_command")
        self.until(lambda: self.root_agent(task), lambda a: a.state == "waiting")
        proc = self.process(task, "running")
        release.touch()
        agent = self.finished(task)
        self.assertEqual([a.end_reason for a in agent.attempts], ["yielded", "finished"])
        result = wire.CommandResult.model_validate(agent.calls[0].outcome.result)
        self.assertEqual((result.process_id, result.outcome, result.exit_code), (proc.id, "exited", 7))
        self.assertEqual(result.output_tail, "first\nsecond\n")
        log = self.owner.read_log(proc.id)
        self.assertTrue(log.eof)
        self.assertEqual(base64.b64decode(log.data_b64), b"first\nsecond\n")

    def launch_pids(self, task: str, process: str) -> tuple[int, int]:
        """Find the shim and its payload using the public launch event and process table."""
        event = next(event for event in self.owner.events(task).events
                     if event.type == "launch_granted" and event.data["process_id"] == process)
        launch = event.data["launch_id"]
        [shim] = self.until(lambda: matching_pids("-m swarm.supervisor.shim", f"--launch {launch}"))
        [payload] = self.until(lambda: matching_pids(parent=shim))
        return shim, payload

    def test_t10_background_payload_survives_all_worker_loss(self) -> None:
        release = self.state / "finish-worker-tool"
        payload_release = self.state / "finish-payload"
        first_turn = [
            call("process_start", "start", argv=self.command(payload_release), lifetime="task"),
            call("wait_for_release", "gate", path=str(release)),
        ]
        self.set_scripts({"keep process": [first_turn, "done"]})
        self.start_server()
        self.start_supervisor()
        task = self.submit("keep process", "process_start", "wait_for_release", "process_read", "process_stop")
        first = self.running_call(task, "gate").attempts[0]
        process = self.process(task, "running")
        before = self.launch_pids(task, process.id)
        prefix = self.until(lambda: self.owner.read_log(process.id, max_bytes=6),
                            lambda chunk: chunk.next_offset == 6)
        self.assertEqual(base64.b64decode(prefix.data_b64), b"first\n")
        # Fill in the real process ID and offset for the turn not played yet.
        self.set_scripts({"keep process": [first_turn, [
            call("process_read", "read", process_id=process.id, offset=prefix.next_offset),
            call("process_stop", "stop", process_id=process.id),
        ], "done"]})
        send(first.pid, signal.SIGKILL)
        self.running_call(task, "gate", attempt=2)
        self.assertEqual(self.launch_pids(task, process.id), before)
        release.touch()
        agent = self.finished(task)
        log = wire.LogChunk.model_validate(next(c for c in agent.calls if c.call_id == "read").outcome.result)
        self.assertEqual((log.offset, base64.b64decode(log.data_b64)), (6, b"second\n"))
        ended = self.process(task, "ended")
        self.assertEqual((ended.id, ended.outcome, ended.signal), (process.id, "signaled", signal.SIGTERM))
        grants = [event for event in self.owner.events(task).events if event.type == "launch_granted"]
        self.assertEqual(len(grants), 1)

    def test_t13_shim_loss_uses_receipt_or_reports_unknown(self) -> None:
        policy = self.state / "policy.json"
        policy.write_text(json.dumps({"programs": {"python3": "local", "sh": "external"}}))
        releases = {name: self.state / f"release-{name}" for name in ("local", "external", "receipt")}
        self.set_scripts({name: [[
            call("run_command", "command", argv=self.command(release, external=name == "external"), timeout_s=45),
        ], "done"] for name, release in releases.items()})
        server = self.start_server(policy=policy)
        self.start_supervisor()
        tasks = {name: self.submit(name, "run_command") for name in releases}
        procs = {name: self.process(task, "running") for name, task in tasks.items()}
        for name in ("local", "external"):
            shim, payload = self.launch_pids(tasks[name], procs[name].id)
            self.addCleanup(send, payload, signal.SIGKILL)
            send(shim, signal.SIGKILL)
            send(payload, signal.SIGKILL)
        proc = procs["receipt"]
        shim, _ = self.launch_pids(tasks["receipt"], proc.id)
        receipt = self.state / "processes" / proc.id / "exit.json"
        # Stop HTTP consumption, not the payload. The real shim writes the receipt.
        send(server.pid, signal.SIGSTOP)
        try:
            releases["receipt"].touch()
            self.until(receipt.exists, timeout=5)
            send(shim, signal.SIGKILL)
        finally:
            send(server.pid, signal.SIGCONT)
        for name in ("local", "external"):
            self.assertEqual(self.process(tasks[name], "ended").outcome, "unknown")
        local = self.finished(tasks["local"])
        self.assertEqual(wire.CommandResult.model_validate(local.calls[0].outcome.result).outcome, "interrupted")
        external = self.until(lambda: self.root_agent(tasks["external"]), lambda a: a.state == "blocked")
        self.assertEqual(external.calls[0].state, "unknown")
        recovered = self.finished(tasks["receipt"])
        result = wire.CommandResult.model_validate(recovered.calls[0].outcome.result)
        self.assertEqual((result.outcome, result.exit_code, result.output_tail), ("exited", 7, "first\nsecond\n"))
