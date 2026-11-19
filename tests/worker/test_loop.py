"""Drive a real worker loop with a FakeModel and an in-memory agent API."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import ai
from ai.testing import FakeModel
from ai.types.messages import Message, ToolCallPart

from swarm import wire
from swarm.worker.external import run_external_agent
from swarm.worker.lease import Lease
from swarm.worker.loop import run_loop_agent
from swarm.worker.tools import Registry, ToolEntry, builtin_registry, current_call


def tool_call(name: str, call_id: str, **args: wire.JSON) -> ToolCallPart:
    return ToolCallPart(tool_name=name, tool_call_id=call_id, tool_args=json.dumps(args))


class MemoryClient:
    """A stand-in for the AgentClient's public methods; no server implementation calls."""

    def __init__(self, sandbox: str, *, tools: list[str], refuse: bool = False, over: bool = False) -> None:
        self.spec = wire.TaskSpec(prompt="do the work", tools=tools, system="Be brief")
        self.sandbox = sandbox
        self.history: list[wire.StoredMessage] = []
        self.pending: dict[str, wire.CallView] = {}
        self.memory: dict[str, str] = {}
        self.log: list[tuple] = []
        self.result: wire.JSON = None
        self.attempt = 1
        self.refuse = refuse
        self.over = over
        self.admitted_seq: int | None = None
        self.complete_on_yield: bool = False

    def make_claim(self) -> wire.Claim:
        return wire.Claim(agent_id="agent", task_id="task", attempt=self.attempt, lease_ttl_s=5,
                          spec=self.spec, history=list(self.history), next_seq=len(self.history),
                          pending_calls=list(self.pending.values()), memory=dict(self.memory), sandbox_path=self.sandbox)

    def heartbeat(self, agent_id: str, attempt: int, timeout_s: float = 1) -> wire.HeartbeatReply:
        return wire.HeartbeatReply(lease_until=0)

    def commit_step(self, agent_id: str, step: wire.StepRequest, *, deadline: float) -> wire.StepReply:
        assert step.expected_seq == len(self.history)
        if step.kind == "model":
            external_final = self.spec.kind == "external" and not step.calls and self.history[-1].role == "tool"
            assert external_final or self.admitted_seq == step.expected_seq
            self.admitted_seq = None
            self.pending = {}
            for call in step.calls:
                outcome = None if call.error is None else wire.CallResult(result=call.error, is_error=True)
                self.pending[call.call_id] = wire.CallView(
                    agent_id=agent_id, step_seq=step.expected_seq, call_id=call.call_id,
                    tool=call.tool, args=call.args, safety=call.safety,
                    state="done" if outcome else "planned", outcome=outcome,
                )
        if step.kind == "tools":
            assert all(call.state == "done" for call in self.pending.values())
            assert step.result_call_ids == list(self.pending)
            self.pending.clear()
        self.history.append(wire.StoredMessage(seq=step.expected_seq, attempt=step.attempt,
                                              role=step.message["role"], message=step.message))
        self.log.append(("commit", step.kind, step.expected_seq))
        return wire.StepReply(next_seq=len(self.history), state="failed" if self.over and step.kind == "model" else "leased")

    def start_call(self, agent_id: str, attempt: int, step_seq: int, call_id: str, *, deadline: float) -> wire.CallStartReply:
        call = self.pending[call_id]
        assert call.step_seq == step_seq == len(self.history) - 1
        self.log.append(("start", call_id))
        if call.state == "done":
            return wire.CallStartReply(action="use", outcome=call.outcome)
        if call.state == "unknown":
            return wire.CallStartReply(action="blocked")
        if call.tool in ("run_child", "run_command"):
            self.pending[call_id] = call.model_copy(update={"state": "waiting"})
            return wire.CallStartReply(action="wait")
        if call.tool == "memory_set":
            self.memory[call.args["key"]] = call.args["value"]
            outcome = wire.CallResult(result={**call.args, "version": 1})
            self.pending[call_id] = call.model_copy(update={"state": "done", "outcome": outcome})
            return wire.CallStartReply(action="use", outcome=outcome)
        assert call.state == "planned"
        self.pending[call_id] = call.model_copy(update={"state": "running"})
        return wire.CallStartReply(action="run")

    def finish_call(self, agent_id: str, attempt: int, step_seq: int, call_id: str, outcome: str, result: wire.JSON,
                    is_error: bool, *, deadline: float) -> wire.CallView:
        call = self.pending[call_id]
        assert call.state == "running"
        value = wire.CallResult(result=result, is_error=is_error) if outcome == "done" else None
        saved = call.model_copy(update={"state": outcome, "outcome": value})
        self.pending[call_id] = saved
        self.log.append(("finish_call", call_id, outcome))
        return saved

    def yield_agent(self, agent_id: str, attempt: int, *, deadline: float) -> wire.YieldReply:
        self.log.append(("yield",))
        if self.complete_on_yield:
            for call_id, call in self.pending.items():
                assert call.state == "waiting"
                result = wire.CallResult(result=f"finished {call_id}")
                self.pending[call_id] = call.model_copy(update={"state": "done", "outcome": result})
            return wire.YieldReply(action="continue", state="leased")
        blocked = any(call.state == "unknown" for call in self.pending.values())
        return wire.YieldReply(action="released", state="blocked" if blocked else "waiting")

    def admit_model_call(self, agent_id: str, attempt: int, step_seq: int, estimated_tokens: int,
                         *, deadline: float) -> wire.ModelCallReply:
        assert step_seq == len(self.history) and not self.pending
        assert estimated_tokens >= self.spec.max_output_tokens
        self.log.append(("admit", step_seq))
        self.admitted_seq = step_seq
        return wire.ModelCallReply(admitted=not self.refuse, remaining_tokens=10000)

    def finish_agent(self, agent_id: str, attempt: int, status: str, result: wire.JSON, *,
                     deadline: float) -> wire.AgentStateReply:
        assert status == "done" and not self.pending
        self.result = result
        self.log.append(("finish",))
        return wire.AgentStateReply(state="done")


class WorkerLoopTest(unittest.IsolatedAsyncioTestCase):
    async def run_agent(self, client: MemoryClient, model: FakeModel, registry: Registry | None = None) -> None:
        claim = client.make_claim()
        lease = Lease(client, claim)
        self.addCleanup(lease.close)
        lease.start()
        try:
            await run_loop_agent(lease, claim, model, registry or builtin_registry())
        finally:
            lease.close()

    async def test_resume_from_each_committed_step_and_tool_result(self) -> None:
        script = [ai.user_message("do the work"), ai.assistant_message(
            tool_call("write_file", "write", path="note", content="hello"),
            tool_call("memory_set", "memory", key="topic", value="notes"),
        ), ai.assistant_message("done")]
        with tempfile.TemporaryDirectory() as original:
            baseline = MemoryClient(original, tools=["write_file", "memory_set"])
            await self.run_agent(baseline, FakeModel(script))
            self.assertEqual(baseline.log, [
                ("commit", "user", 0), ("admit", 1), ("commit", "model", 1), ("start", "write"),
                ("finish_call", "write", "done"), ("start", "memory"), ("commit", "tools", 2),
                ("admit", 3), ("commit", "model", 3), ("finish",),
            ])
        for cut, saved_write in [(0, False), (1, False), (2, False), (2, True), (3, False), (4, False)]:
            with self.subTest(cut=cut, saved_write=saved_write), tempfile.TemporaryDirectory() as sandbox:
                client = MemoryClient(sandbox, tools=baseline.spec.tools)
                client.history = baseline.history[:cut]
                client.attempt = 2
                if cut == 2:
                    for call_id, tool, args, safety in [
                        ("write", "write_file", {"path": "note", "content": "hello"}, "idempotent"),
                        ("memory", "memory_set", {"key": "topic", "value": "notes"}, "server"),
                    ]:
                        saved = call_id == "write" and saved_write
                        client.pending[call_id] = wire.CallView(
                            agent_id="agent", step_seq=1, call_id=call_id, tool=tool, args=args, safety=safety,
                            state="done" if saved else "planned", outcome=wire.CallResult(result="note") if saved else None,
                        )
                if saved_write or cut >= 3:
                    Path(sandbox, "note").write_text("hello")
                if cut >= 3:
                    client.memory = dict(baseline.memory)
                model = FakeModel(script)
                await self.run_agent(client, model)
                self.assertEqual(client.result, "done")
                self.assertEqual(Path(sandbox, "note").read_text(), "hello")
                self.assertEqual(len(client.history), 4)
                self.assertEqual([m.role for m in client.history], ["user", "assistant", "tool", "assistant"])
                self.assertEqual(sum(entry[0] == "finish_call" for entry in client.log), int(cut <= 2 and not saved_write))
                self.assertEqual(len(model.calls), int(cut <= 1) + int(cut <= 3))
                if model.calls:
                    self.assertIn('"topic": "notes"', model.calls[-1][0].text)
                saved = Message.model_validate(client.history[2].message)
                self.assertEqual([part.tool_call_id for part in saved.tool_results], ["write", "memory"])

    async def test_nonbarrier_calls_start_before_yield_and_continue(self) -> None:
        with tempfile.TemporaryDirectory() as sandbox:
            client = MemoryClient(sandbox, tools=["run_child"])
            client.complete_on_yield = True
            reply = ai.assistant_message(tool_call("run_child", "one", prompt="one"),
                                         tool_call("run_child", "two", prompt="two"))
            model = FakeModel([ai.user_message("do the work"), reply, ai.assistant_message("done")])
            await self.run_agent(client, model)
            self.assertEqual(client.result, "done")
            self.assertEqual(client.log[3:9], [("start", "one"), ("start", "two"), ("yield",),
                                               ("start", "one"), ("start", "two"), ("commit", "tools", 2)])

    async def test_barrier_yields_without_starting_later_call(self) -> None:
        with tempfile.TemporaryDirectory() as sandbox:
            client = MemoryClient(sandbox, tools=["run_command", "write_file"])
            reply = ai.assistant_message(tool_call("run_command", "command", argv=["cat"]),
                                         tool_call("write_file", "write", path="note", content="hello"))
            await self.run_agent(client, FakeModel([ai.user_message("do the work"), reply]))
            self.assertEqual(client.log[3:], [("start", "command"), ("yield",)])
            self.assertFalse(Path(sandbox, "note").exists())

    async def test_budget_refusal_and_overage_do_not_start_tool(self) -> None:
        for refuse, over, calls in [(True, False, 0), (False, True, 1)]:
            with self.subTest(refuse=refuse, over=over), tempfile.TemporaryDirectory() as sandbox:
                client = MemoryClient(sandbox, tools=["write_file"], refuse=refuse, over=over)
                reply = ai.assistant_message(tool_call("write_file", "write", path="note", content="hello"))
                model = FakeModel([ai.user_message("do the work"), reply])
                await self.run_agent(client, model)
                self.assertEqual(len(model.calls), calls)
                self.assertFalse(any(entry[0] == "start" for entry in client.log))
                self.assertFalse(Path(sandbox, "note").exists())

    async def test_unsafe_exception_yields_unknown(self) -> None:
        @ai.tool
        async def uncertain() -> str:
            """Raise after inspecting the active call."""
            if current_call().call_id != "unsafe":
                raise AssertionError("Wrong tool context")
            raise RuntimeError("Uncertain effect")

        with tempfile.TemporaryDirectory() as sandbox:
            client = MemoryClient(sandbox, tools=["uncertain"])
            script = [ai.user_message("do the work"), ai.assistant_message(tool_call("uncertain", "unsafe"))]
            await self.run_agent(client, FakeModel(script), Registry([ToolEntry(uncertain, "unsafe")]))
            self.assertEqual(client.log[3:], [("start", "unsafe"), ("finish_call", "unsafe", "unknown"), ("yield",)])
            self.assertEqual(client.pending["unsafe"].state, "unknown")

    async def test_invalid_arguments_and_symlink_are_errors(self) -> None:
        with tempfile.TemporaryDirectory() as sandbox:
            Path(sandbox, "alias").symlink_to("/etc/hosts")
            client = MemoryClient(sandbox, tools=["read_file", "write_file"])
            reply = ai.assistant_message(tool_call("write_file", "invalid", path="note"),
                                         tool_call("read_file", "symlink", path="alias"))
            script = [ai.user_message("do the work"), reply, ai.assistant_message("done")]
            await self.run_agent(client, FakeModel(script))
            results = Message.model_validate(client.history[2].message).tool_results
            self.assertTrue(all(result.is_error for result in results))
            self.assertIn("Invalid arguments", results[0].result)
            self.assertIn(("finish_call", "symlink", "done"), client.log)
            self.assertNotIn(("finish_call", "invalid", "done"), client.log)

    async def test_external_resume_uses_committed_turn_without_starting_fx(self) -> None:
        with tempfile.TemporaryDirectory() as sandbox:
            client = MemoryClient(sandbox, tools=[])
            client.spec = client.spec.model_copy(update={"kind": "external", "external": wire.ExternalSpec()})
            user = ai.user_message(client.spec.prompt)
            planned = ai.assistant_message(tool_call("cli_turn", "turn"))
            client.history = [wire.StoredMessage(seq=seq, attempt=1, role=message.role,
                                                 message=message.model_dump(mode="json"))
                              for seq, message in enumerate((user, planned))]
            client.pending["turn"] = wire.CallView(
                agent_id="agent", step_seq=1, call_id="turn", tool="cli_turn", args={},
                safety="unsafe", state="done", outcome=wire.CallResult(result="saved result"),
            )
            client.attempt = 2
            claim = client.make_claim()
            lease = Lease(client, claim)
            lease.start()
            try:
                await run_external_agent(lease, claim)
            finally:
                lease.close()
            self.assertEqual(client.log, [("start", "turn"), ("commit", "tools", 2),
                                          ("commit", "model", 3), ("finish",)])
            self.assertEqual(client.result, "saved result")
