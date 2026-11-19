"""HTTP proofs for the durable server contract."""

import json
import subprocess
import sys
import time
import unittest

from tests.server.harness import ServerHarness


class ServerProof(ServerHarness):
    def test_t4_stale_attempt_changes_nothing(self):
        created = self.create()
        first = self.claim()
        self.wait_agent(created["task_id"], created["agent_id"], "ready")
        second = self.claim()
        self.assertEqual(second["attempt"], first["attempt"] + 1)
        before = self.task(created["task_id"])
        error = self.request("commit_step", {"attempt": first["attempt"], "request_id": self.id(),
                    "kind": "user", "expected_seq": 0, "message": {"role": "user"}},
                    agent_id=first["agent_id"], expected=409)
        self.assertEqual(error["error"], "stale")
        self.assertEqual(before, self.task(created["task_id"]))
        self.assertEqual(self.request("get_history", agent_id=first["agent_id"])["messages"], [])

    def test_t5_replay_and_terminal_yield(self):
        request_id = self.id()
        body = {"request_id": request_id, "spec": {"prompt": "test", "tools": ["run_child"]}}
        created = self.request("create_task", body)
        self.assertEqual(created, self.request("create_task", body))
        changed = {**body, "spec": {"prompt": "different"}}
        self.assertEqual(self.request("create_task", changed, expected=422)["error"], "invalid")
        claim_id = self.id()
        claim = self.claim(claim_id)
        self.assertEqual(claim, self.claim(claim_id))
        self.user_step(claim)
        self.model_step(claim, [self.planned("run_child", "server", {"prompt": "child"})])
        self.assertEqual(self.call("start_call", claim)["action"], "wait")
        data = {"attempt": claim["attempt"], "request_id": self.id()}
        reply = self.request("yield_agent", data, agent_id=claim["agent_id"])
        self.assertEqual(reply["action"], "released")
        self.assertEqual(reply, self.request("yield_agent", data, agent_id=claim["agent_id"]))
        self.assertEqual(self.agent(created["task_id"], claim["agent_id"])["state"], "waiting")

    def test_t6_unsafe_loss_resolve_and_cancel(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                created = self.create(tools=["risky"])
                claim = self.claim()
                self.user_step(claim)
                self.model_step(claim, [self.planned("risky", "unsafe")])
                self.assertEqual(self.call("start_call", claim)["action"], "run")
                agent = self.wait_agent(created["task_id"], claim["agent_id"], "blocked")
                self.assertEqual(agent["calls"][0]["state"], "unknown")
                path = dict(agent_id=claim["agent_id"], step_seq=1, call_id="call")
                if cancel:
                    self.request("cancel_task", {"request_id": self.id()}, task_id=created["task_id"])
                    self.assertEqual(self.request("resolve_call", {"request_id": self.id(), "result": "ok"},
                                                  expected=409, **path)["error"], "conflict")
                else:
                    resolved = self.request("resolve_call", {"request_id": self.id(), "result": "ok"}, **path)
                    self.assertEqual(resolved["state"], "done")
                    resumed = self.wait_for(lambda: self.claim(expected=(200, 204)) if self.agent(
                        created["task_id"], claim["agent_id"])["state"] == "ready" else None)
                    self.finish(resumed, call_ids=["call"])
                    self.assertEqual(self.task(created["task_id"])["state"], "done")

    def test_t7_poison_child_and_unactivated_loss(self):
        created = self.create(tools=["run_child"])
        parent = self.claim()
        self.user_step(parent)
        self.model_step(parent, [self.planned("run_child", "server", {"prompt": "child"})])
        self.assertEqual(self.call("start_call", parent)["action"], "wait")
        self.assertEqual(self.write("yield_agent", parent)["action"], "released")
        child = self.claim()
        self.assertNotEqual(child["agent_id"], parent["agent_id"])
        agent = self.wait_agent(created["task_id"], child["agent_id"], "ready")
        self.assertEqual(agent["strikes"], 0)
        for strike in range(1, 4):
            child = self.wait_for(lambda: self.claim(expected=(200, 204)) if self.agent(
                created["task_id"], child["agent_id"])["state"] == "ready" else None)
            self.request("heartbeat", {"attempt": child["attempt"]}, agent_id=child["agent_id"])
            state = "failed" if strike == 3 else "ready"
            agent = self.wait_agent(created["task_id"], child["agent_id"], state)
            self.assertEqual(agent["strikes"], strike)
        self.assertEqual(agent["fail_reason"], "poison")
        parent_view = self.wait_agent(created["task_id"], parent["agent_id"], "ready")
        self.assertEqual(parent_view["calls"][0]["state"], "done")
        self.assertTrue(parent_view["calls"][0]["outcome"]["is_error"])

    def test_t12_policy_refusal_and_local_class(self):
        policy = self.state / "policy.json"
        policy.write_text(json.dumps({"programs": {"echo": "local"}}))
        self.stop_server()
        self.start_server(policy=policy)
        created = self.create(tools=["run_command"])
        claim = self.claim()
        self.user_step(claim)
        self.model_step(claim, [self.planned("run_command", "server", {"argv": ["not_allowed"]})])
        result = self.call("start_call", claim)
        self.assertEqual(result["action"], "use")
        self.assertTrue(result["outcome"]["is_error"])
        self.assertEqual(self.request("list_processes", task_id=created["task_id"])["processes"], [])
        self.write("commit_step", claim, kind="tools", expected_seq=2,
                   message={"role": "tool"}, result_call_ids=["call"])
        self.model_step(claim, [self.planned("run_command", "server", {"argv": ["echo", "hi"]})], seq=3)
        self.assertEqual(self.call("start_call", claim, seq=3)["action"], "wait")
        processes = self.request("list_processes", task_id=created["task_id"])["processes"]
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0]["effect_class"], "local")

    def test_t14_budget_admission_and_usage(self):
        created = self.create(budget_tokens=5)
        claim = self.claim()
        self.user_step(claim)
        self.assertFalse(self.write("admit_model_call", claim, step_seq=1, estimated_tokens=10)["admitted"])
        self.assertEqual(self.agent(created["task_id"], claim["agent_id"])["fail_reason"], "budget")
        created = self.create(budget_tokens=20, tools=["memory_set"])
        claim = self.claim()
        self.user_step(claim)
        reply = self.model_step(claim, [self.planned("memory_set", "server", {"key": "k", "value": "v"})],
                                usage={"input_tokens": 15, "output_tokens": 10})
        self.assertEqual(reply["state"], "failed")
        agent = self.agent(created["task_id"], claim["agent_id"])
        self.assertEqual(agent["fail_reason"], "budget")
        self.assertNotIn("k", agent["memory"])
        self.assertEqual(agent["calls"][0]["state"], "done")
        self.assertTrue(agent["calls"][0]["outcome"]["is_error"])

    def command_process(self):
        created = self.create(tools=["run_command"])
        claim = self.claim()
        self.user_step(claim)
        self.model_step(claim, [self.planned("run_command", "server", {"argv": ["echo", "ok"]})])
        self.assertEqual(self.call("start_call", claim)["action"], "wait")
        return created, claim

    def next_launch(self):
        return self.request("next_launch", {"runner_id": "runner", "wait_s": 0})

    def test_t16_launch_grants_deadlines_and_restart(self):
        created, claim = self.command_process()
        issued = self.next_launch()
        old_url = self.url
        self.stop_server()
        self.start_server()
        self.assertEqual(self.url, old_url)
        replacement = self.next_launch()
        self.assertNotEqual(issued["launch_id"], replacement["launch_id"])
        path = {"launch_id": issued["launch_id"], "token": issued["secret"]}
        self.assertFalse(self.request("grant_launch", {"pid": 11111}, **path)["granted"])
        self.assertTrue(self.request("launch_heartbeat", {"log_bytes": 0}, **path)["stop"])
        time.sleep(0.7)
        path = {"launch_id": replacement["launch_id"], "token": replacement["secret"]}
        self.assertFalse(self.request("grant_launch", {"pid": 22222}, **path)["granted"])
        current = self.wait_for(lambda: self.request("next_launch", {"runner_id": "runner", "wait_s": 0},
                                      expected=(200, 204)))
        path = {"launch_id": current["launch_id"], "token": current["secret"]}
        self.assertTrue(self.request("grant_launch", {"pid": 33333}, **path)["granted"])
        self.assertTrue(self.request("grant_launch", {"pid": 33333}, **path)["granted"])
        self.assertFalse(self.request("grant_launch", {"pid": 44444}, **path)["granted"])
        processes = self.request("list_processes", task_id=created["task_id"])["processes"]
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0]["state"], "running")

    def test_launch_exit_fills_yielded_command_with_tail(self):
        created, claim = self.command_process()
        released = self.write("yield_agent", claim)
        self.assertEqual((released["action"], released["state"]), ("released", "waiting"))
        launch = self.next_launch()
        path = {"launch_id": launch["launch_id"], "token": launch["secret"]}
        self.assertTrue(self.request("grant_launch", {"pid": 33333}, **path)["granted"])
        report = {"exit_code": 7, "log_bytes": 11, "truncated_bytes": 2, "output_tail": "hello world"}
        self.assertTrue(self.request("report_exit", report, **path)["ok"])
        self.assertTrue(self.request("report_exit", report, **path)["ok"])
        error = self.request("report_exit", {**report, "exit_code": 8}, expected=409, **path)
        self.assertEqual(error["error"], "conflict")
        process = self.request("list_processes", task_id=created["task_id"])["processes"][0]
        self.assertEqual((process["state"], process["outcome"], process["exit_code"]), ("ended", "exited", 7))
        agent = self.wait_agent(created["task_id"], claim["agent_id"], "ready")
        call = agent["calls"][0]
        self.assertEqual(call["state"], "done")
        self.assertFalse(call["outcome"]["is_error"])
        result = call["outcome"]["result"]
        self.assertEqual(result["process_id"], launch["process_id"])
        self.assertEqual((result["outcome"], result["exit_code"]), ("exited", 7))
        self.assertEqual((result["log_bytes"], result["truncated_bytes"], result["output_tail"]),
                         (11, 2, "hello world"))
        self.assertIsNone(self.request("next_launch", {"runner_id": "runner", "wait_s": 0}, expected=204))
        resumed = self.claim()
        self.finish(resumed, call_ids=["call"])
        self.assertEqual(self.task(created["task_id"])["state"], "done")

    def test_restart_waits_for_active_worker_before_copying(self):
        created = self.create()
        first = self.claim()
        self.request("heartbeat", {"attempt": first["attempt"]}, agent_id=first["agent_id"])
        self.stop_server()
        self.start_server()
        self.assertIsNone(self.claim(expected=204))
        recovered = self.agent(created["task_id"], first["agent_id"])
        self.assertEqual(recovered["strikes"], 0)
        self.assertEqual(recovered["attempts"][0]["end_reason"], "server_restart")
        second = self.wait_for(lambda: self.claim(expected=(200, 204)))
        self.assertEqual(second["attempt"], first["attempt"] + 1)
        self.assertNotEqual(second["sandbox_path"], first["sandbox_path"])

    def test_t16_cli_reuses_ephemeral_port(self):
        self.stop_server()
        self.start_server(cli=True, cli_port=0)
        url, tokens = self.url, self.tokens
        self.stop_server()
        self.start_server(cli=True)
        self.assertEqual(self.url, url)
        self.assertEqual(self.tokens, tokens)
        self.assertTrue(self.request("health")["ok"])

    def test_t17_import_without_ai(self):
        code = ("import sys; import swarm.server, swarm.client, swarm.supervisor; "
                "import swarm.server.app, swarm.client.api, swarm.supervisor.runner; "
                "assert not any(n == 'ai' or n.startswith('ai.') for n in sys.modules)")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_t18_cancel_fences_worker_and_stops_process(self):
        created, claim = self.command_process()
        task = self.request("cancel_task", {"request_id": self.id()}, task_id=created["task_id"])
        self.assertEqual(task["state"], "cancelled")
        agent = next(a for a in task["agents"] if a["id"] == claim["agent_id"])
        self.assertEqual((agent["state"], agent["fail_reason"]), ("failed", "cancelled"))
        processes = self.request("list_processes", task_id=created["task_id"])["processes"]
        self.assertEqual(len(processes), 1)
        self.assertEqual((processes[0]["desired"], processes[0]["stop_reason"]), ("stopped", "cancelled"))
        error = self.request("heartbeat", {"attempt": claim["attempt"]},
                             agent_id=claim["agent_id"], expected=409)
        self.assertEqual(error["error"], "stale")


if __name__ == "__main__":
    unittest.main()
