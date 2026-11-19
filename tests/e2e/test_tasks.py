"""Task durability across real server, supervisor, and worker processes."""

import signal
from pathlib import Path

from tests.fakes import call
from tests.helpers import SwarmTest, send


class TaskTest(SwarmTest):
    def test_t1_write_memory_and_finish(self) -> None:
        self.set_scripts({"write and remember": [[
            call("write_file", "write", path="note.txt", content="durable\n"),
            call("memory_set", "remember", key="topic", value="durability"),
        ], "done"]})
        self.start_server()
        self.start_supervisor()
        result = self.owner_cli("submit", "--prompt", "write and remember", "--tools", "write_file,memory_set", "--wait")
        self.assertEqual(result["state"], "done")
        agent = self.finished(result["id"])
        self.assertEqual(agent.memory, {"topic": "durability"})
        self.assertEqual(len(agent.attempts), 1)
        self.assertEqual((Path(agent.attempts[0].sandbox_path) / "note.txt").read_text(), "durable\n")
        messages = self.owner.history(agent.id).messages
        self.assertEqual([(m.seq, m.role) for m in messages], [(0, "user"), (1, "assistant"), (2, "tool"), (3, "assistant")])
        self.assertEqual([c.state for c in agent.calls], ["done", "done"])
        self.assertEqual(self.owner_cli("show", result["id"])["root_agent_id"], agent.id)

    def interrupted_task(self, tool: str = "wait_for_release") -> tuple[str, Path]:
        """Start a scripted task whose first attempt waits inside the given tool."""
        release = self.state / "release"
        self.set_scripts({"work": [[call(tool, "gate", path=str(release))], "done"]})
        self.start_server()
        return self.submit("work", tool), release

    def test_t2_killed_worker_retries_one_tool_call(self) -> None:
        task, release = self.interrupted_task()
        self.start_supervisor()
        first = self.running_call(task, "gate").attempts[0]
        send(first.pid, signal.SIGKILL)
        second = self.running_call(task, "gate", attempt=2).attempts[1]
        self.assertNotEqual(first.pid, second.pid)
        release.touch()
        agent = self.finished(task)
        self.assertEqual(agent.attempts[0].end_reason, "lost")
        self.assertEqual([(c.call_id, c.runs, c.state) for c in agent.calls], [("gate", 2, "done")])
        history = self.owner.history(agent.id).messages
        self.assertEqual([(m.seq, m.attempt) for m in history], [(0, 1), (1, 1), (2, 2), (3, 2)])
