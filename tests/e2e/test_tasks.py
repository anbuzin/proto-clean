"""Task durability across real server, supervisor, and worker processes."""

from pathlib import Path

from tests.fakes import call
from tests.helpers import SwarmTest


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
