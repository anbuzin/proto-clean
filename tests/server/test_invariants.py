"""Cover the transactional checks that must hold even when the reaper is idle."""

import time

from .harness import ServerHarness


class InvariantTests(ServerHarness):
    def test_i5_a_new_request_id_cannot_repeat_a_run_grant(self) -> None:
        task = self.create(tools=["local_tool"])
        claim = self.claim()
        self.user_step(claim)
        self.model_step(claim, [self.planned("local_tool", "idempotent")])
        self.assertEqual(self.call("start_call", claim)["action"], "run")
        self.assertEqual(self.call("start_call", claim)["action"], "stop")
        agent = self.agent(task["task_id"], claim["agent_id"])
        self.assertEqual(agent["calls"][0]["runs"], 1)

    def issue_command(self) -> tuple[dict, dict, dict]:
        """Yield on a real command call and return its first launch order."""
        task = self.create(tools=["run_command"])
        claim = self.claim()
        self.user_step(claim)
        self.model_step(claim, [self.planned("run_command", "server", {"argv": ["echo", "ok"]})])
        self.assertEqual(self.call("start_call", claim)["action"], "wait")
        self.write("yield_agent", claim)
        launch = self.request("next_launch", {"runner_id": "runner", "wait_s": 0})
        return task, claim, launch

    def test_i12_grant_checks_deadline_without_reaper(self) -> None:
        self.stop_server()
        self.start_server(reaper_interval=30)
        task, _, launch = self.issue_command()
        time.sleep(launch["grant_deadline_s"] + 0.05)
        process = self.request("list_processes", task_id=task["task_id"])["processes"][0]
        self.assertEqual(process["state"], "launching")
        path = {"launch_id": launch["launch_id"], "token": launch["secret"]}
        self.assertFalse(self.request("grant_launch", {"pid": 1234}, **path)["granted"])
        process = self.request("list_processes", task_id=task["task_id"])["processes"][0]
        self.assertEqual(process["state"], "launching")

    def test_exit_validation_precedes_launch_lookup(self) -> None:
        path = {"launch_id": "absent", "token": "unused"}
        for report in ({}, {"exit_code": 0, "signal": 15}, {"exit_code": -1}, {"signal": 0},
                       {"exit_code": 0, "log_bytes": -1}):
            with self.subTest(report=report):
                error = self.request("report_exit", report, expected=422, validate=False, **path)
                self.assertEqual(error["error"], "invalid")

    def test_owner_stop_is_an_external_cut_even_if_payload_exits_zero(self) -> None:
        task, claim, launch = self.issue_command()
        path = {"launch_id": launch["launch_id"], "token": launch["secret"]}
        self.assertTrue(self.request("grant_launch", {"pid": 1234}, **path)["granted"])
        self.request("stop_process", {"request_id": self.id()}, process_id=launch["process_id"])
        self.assertTrue(self.request("report_exit", {"exit_code": 0}, **path)["ok"])
        agent = self.wait_agent(task["task_id"], claim["agent_id"], "blocked")
        self.assertEqual(agent["calls"][0]["state"], "unknown")

    def test_never_granted_command_is_an_error_result(self) -> None:
        task, claim, launch = self.issue_command()
        for attempt in range(3):
            if attempt:
                launch = self.request("next_launch", {"runner_id": "runner", "wait_s": 0})
            self.request("report_exit", {"started": False}, launch_id=launch["launch_id"], token=launch["secret"])
        agent = self.wait_agent(task["task_id"], claim["agent_id"], "ready")
        outcome = agent["calls"][0]["outcome"]
        self.assertEqual(outcome["result"]["outcome"], "not_started")
        self.assertTrue(outcome["is_error"])

    def test_cancelled_child_does_not_settle_its_parent_to_ready(self) -> None:
        task = self.create(tools=["run_child"])
        claim = self.claim()
        self.user_step(claim)
        self.model_step(claim, [self.planned("run_child", "server", {"prompt": "child"})])
        self.assertEqual(self.call("start_call", claim)["action"], "wait")
        self.assertEqual(self.write("yield_agent", claim)["state"], "waiting")
        self.request("cancel_task", {"request_id": self.id()}, task_id=task["task_id"])
        events = self.request("list_events")["events"]
        self.assertFalse(any(e["type"] == "agent_settled" and e["data"]["state"] == "ready" for e in events))
