"""Task durability across real server, supervisor, and worker processes."""

import signal
from pathlib import Path

from swarm.client import Rejected
from tests.fakes import call
from tests.helpers import SwarmTest, matching_pids, send


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

    def test_t3_new_supervisor_finishes_after_pool_loss(self) -> None:
        task, release = self.interrupted_task()
        supervisor = self.start_supervisor(workers=2)
        first = self.running_call(task, "gate")
        workers = self.until(lambda: matching_pids(str(self.script), parent=supervisor.pid),
                             lambda pids: len(pids) == 2)
        self.assertIn(first.attempts[0].pid, workers)
        for pid in workers:
            self.addCleanup(send, pid, signal.SIGKILL)
        # Stop the pool before killing its workers, so no replacement can escape.
        supervisor.kill()
        supervisor.wait(timeout=5)
        for pid in workers:
            send(pid, signal.SIGKILL)
        retained = self.owner.get_task(task)
        self.assertEqual([a.id for a in retained.agents], [first.id])
        messages = self.owner.history(first.id).messages
        self.assertEqual([message.seq for message in messages], [0, 1])
        self.assertEqual([c.call_id for c in retained.agents[0].calls], ["gate"])
        self.start_supervisor(workers=2)
        second = self.running_call(task, "gate", attempt=2).attempts[1]
        self.assertNotIn(second.pid, workers)
        release.touch()
        self.finished(task)

    def test_t6_owner_resolves_cut_unsafe_call_but_not_after_cancel(self) -> None:
        task, _ = self.interrupted_task("unsafe_wait_for_release")
        cancelled = self.submit("work", "unsafe_wait_for_release")
        self.start_supervisor(workers=2)
        first = [self.running_call(task_id, "gate") for task_id in (task, cancelled)]
        for agent in first:
            send(agent.attempts[0].pid, signal.SIGKILL)
        blocked = [self.until(lambda: self.root_agent(task_id), lambda agent: agent.state == "blocked")
                   for task_id in (task, cancelled)]
        for agent in blocked:
            self.assertEqual([(c.state, c.runs) for c in agent.calls], [("unknown", 1)])
        self.owner.cancel_task(cancelled)
        with self.assertRaises(Rejected) as refusal:
            self.owner.resolve_call(blocked[1].id, 1, "gate", "too late")
        self.assertEqual(refusal.exception.status, 409)
        reply = self.owner.resolve_call(blocked[0].id, 1, "gate", "owner verified the effect")
        self.assertEqual(reply.state, "done")
        agent = self.finished(task)
        self.assertEqual(agent.attempt, 2)
        self.assertEqual(agent.calls[0].runs, 1)
        self.assertEqual(agent.calls[0].outcome.result, "owner verified the effect")

    def test_t8_both_children_read_the_pinned_parent_copy(self) -> None:
        self.set_scripts({
            "parent": [[
                call("write_file", "seed", path="seed.txt", content="pinned seed"),
                call("run_child", "left", prompt="left child", tools=["read_file"]),
                call("run_child", "right", prompt="right child", tools=["read_file"]),
            ], "done"],
            "left child": [[call("read_file", "left-read", path="seed.txt")], "left done"],
            "right child": [[call("read_file", "right-read", path="seed.txt")], "right done"],
        })
        self.start_server()
        self.start_supervisor(workers=2)
        task = self.submit("parent", "write_file", "run_child", "read_file")
        parent = self.finished(task)
        self.assertEqual([a.end_reason for a in parent.attempts], ["yielded", "finished"])
        self.assertEqual(self.owner.history(parent.id).messages[2].attempt, 2)
        children = {agent.id: agent for agent in self.owner.get_task(task).agents if agent.parent_id == parent.id}
        self.assertEqual(len(children), 2)
        for child_call in parent.calls[1:]:
            child = children[child_call.child_agent_id]
            self.assertEqual((child.state, child.result), ("done", f"{child_call.call_id} done"))
            self.assertEqual(child.calls[0].outcome.result, "pinned seed")
            self.assertEqual(child_call.outcome.result, {"agent_id": child.id, "status": "done", "result": child.result})
        copies = [a.sandbox_path for a in parent.attempts] + [a.attempts[0].sandbox_path for a in children.values()]
        self.assertEqual(len(set(copies)), 4)
