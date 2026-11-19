"""Run real payloads through the runner and launch shim."""

import base64
from pathlib import Path

from swarm import wire
from tests.fakes import call
from tests.helpers import SwarmTest


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
