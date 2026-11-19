"""Test transport semantics on a standard library HTTP server bound to loopback."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path
from threading import Thread
from time import monotonic

from swarm import wire
from swarm.client import AgentClient, HttpClient, LaunchClient, OwnerClient, Rejected, RunnerClient, TaskWaitTimeout, Unreachable
from swarm.owner.__main__ import main as owner_main

Reply = tuple[int, dict] | None
RequestRecord = tuple[str, str, str | None, bytes]


@contextmanager
def server_with(*replies: Reply) -> Iterator[tuple[str, list[RequestRecord]]]:
    responses = iter(replies)
    requests: list[RequestRecord] = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            requests.append((self.command, self.path, self.headers.get("Authorization"), self.rfile.read(size)))
            response = next(responses, None)
            if response is None:
                return
            status, reply = response
            body = json.dumps(reply).encode() if status != 204 else b""
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            if status == 302:
                self.send_header("Location", "/healthz")
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = handle_request

        def log_message(self, format: str, *args: object) -> None:
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            thread.join()


class ClientTest(unittest.TestCase):
    def test_owner_write_replays_byte_for_byte_on_disconnect_and_busy(self) -> None:
        with server_with(None, (503, {"error": "busy"}), (200, {"task_id": "task", "agent_id": "agent"})) as (url, requests):
            owner = OwnerClient(url, "stub-owner")
            task = owner.create_task(wire.TaskSpec(prompt="hello"), request_id="owner-write")
            self.assertEqual(task.task_id, "task")
            self.assertEqual(requests, [requests[0]] * 3)
            self.assertEqual(requests[0][:3], ("POST", "/v1/tasks", "Bearer stub-owner"))
            self.assertEqual(json.loads(requests[0][3])["request_id"], "owner-write")

    def test_claim_replays_lost_response_with_one_request_id(self) -> None:
        claim = wire.Claim(agent_id="alpha", task_id="task", attempt=3, lease_ttl_s=10,
                           spec=wire.TaskSpec(prompt="hello"), history=[], next_seq=0, pending_calls=[],
                           memory={}, sandbox_path="/sandbox")
        with server_with(None, (200, claim.model_dump(mode="json"))) as (url, requests):
            client = AgentClient(url, "stub-worker")
            self.assertEqual(client.claim("worker", 10, ["loop"], wait_s=0), claim)
            self.assertEqual(requests, [requests[0]] * 2)
            self.assertTrue(json.loads(requests[0][3])["request_id"])

    def test_stale_claim_is_not_retried_and_next_claim_uses_fresh_id(self) -> None:
        with server_with((409, {"error": "stale"}), (204, {})) as (url, requests):
            client = AgentClient(url, "stub-worker")
            with self.assertRaises(Rejected) as rejected:
                client.claim("worker", 10, ["loop"], wait_s=0)
            self.assertTrue(rejected.exception.stale)
            self.assertEqual(len(requests), 1)
            self.assertIsNone(client.claim("worker", 10, ["loop"], wait_s=0))
            self.assertNotEqual(*(json.loads(record[3])["request_id"] for record in requests))

    def test_agent_retry_ends_at_deadline_without_changing_the_body(self) -> None:
        with server_with(None, None, None) as (url, requests):
            client = AgentClient(url, "stub-worker")
            with self.assertRaises(Unreachable):
                client.admit_model_call("agent", 2, 1, 10, deadline=monotonic() + 0.25)
            self.assertGreaterEqual(len(requests), 1)
            self.assertEqual(requests, [requests[0]] * len(requests))
            self.assertEqual(json.loads(requests[0][3])["attempt"], 2)

    def test_heartbeat_and_runner_poll_do_not_retry(self) -> None:
        with server_with(None, None, None) as (url, requests):
            with self.assertRaises(Unreachable):
                AgentClient(url, "stub-worker").heartbeat("alpha", 1)
            with self.assertRaises(Unreachable):
                RunnerClient(url, "stub-runner").next_launch("runner", wait_s=0)
            self.assertIsNone(LaunchClient(url, "launch", "stub-secret").heartbeat(0, 0))
            self.assertEqual([record[1] for record in requests], [
                "/v1/agents/alpha/heartbeat", "/v1/runner/launches", "/v1/launches/launch/heartbeat",
            ])

    def test_launch_grant_retries_same_payload(self) -> None:
        with server_with((503, {"error": "busy"}), (200, {"granted": True})) as (url, requests):
            client = LaunchClient(url, "launch", "stub-secret")
            self.assertTrue(client.grant(44, deadline=monotonic() + 2).granted)
            self.assertEqual(requests, [("POST", "/v1/launches/launch/grant", "Bearer stub-secret", b'{"pid":44}')] * 2)

    def test_http_quotes_path_and_query_and_omits_health_auth(self) -> None:
        with server_with((200, {"messages": [], "next_seq": 15}),
                         (200, {"ok": True, "pid": 4, "started_at": 0})) as (url, requests):
            client = OwnerClient(url, "stub-owner")
            self.assertEqual(client.history("a/b", after_seq=14, limit=2).next_seq, 15)
            self.assertTrue(client.health().ok)
            self.assertEqual(requests, [
                ("GET", "/v1/agents/a%2Fb/history?after_seq=14&limit=2", "Bearer stub-owner", b""),
                ("GET", "/healthz", None, b""),
            ])

    def test_client_disallows_http_redirect_to_protect_bearer_token(self) -> None:
        with server_with((302, {}), (200, {"ok": True, "pid": 4, "started_at": 0})) as (url, requests):
            with self.assertRaises(Rejected) as rejected:
                HttpClient(url, "stub-owner").call("get_task", task_id="task")
            self.assertEqual(rejected.exception.status, 302)
            self.assertEqual(len(requests), 1)

    def test_owner_cli_submit_passes_request_id_and_prints_json(self) -> None:
        reply = {"task_id": "task", "agent_id": "alpha"}
        with server_with((200, reply)) as (url, requests), tempfile.TemporaryDirectory() as state:
            Path(state, "server.json").write_text(json.dumps({"url": url}))
            Path(state, "tokens.json").write_text(json.dumps({"owner": "stub-owner"}))
            output = StringIO()
            with redirect_stdout(output):
                result = owner_main(["--state", state, "submit", "--prompt", "hello", "--request-id", "exact-id"])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue()), reply)
            self.assertEqual(json.loads(requests[0][3])["request_id"], "exact-id")

    def test_owner_cli_uses_url_credentials_and_writes_binary_log(self) -> None:
        reply = {"data_b64": "AP8K", "offset": 2, "next_offset": 5, "eof": True}
        with server_with((200, reply), (200, {"messages": [], "next_seq": 8})) as (url, requests):
            env = os.environ | {"SWARM_OWNER_TOKEN": "stub-owner"}
            cli = [sys.executable, "-m", "swarm.owner", "--url", url]
            log = subprocess.run([*cli, "log", "proc", "--offset", "2"], env=env, capture_output=True, timeout=5)
            self.assertEqual(log.returncode, 0, log.stderr)
            self.assertEqual(log.stdout, b"\x00\xff\n")
            page = subprocess.run([*cli, "history", "ag", "--after-seq", "7", "--limit", "3"],
                                  env=env, capture_output=True, timeout=5)
            self.assertEqual(page.returncode, 0, page.stderr)
            self.assertEqual(json.loads(page.stdout)["next_seq"], 8)
            self.assertEqual(requests[0][2], "Bearer stub-owner")
            self.assertIn("after_seq=7&limit=3", requests[1][1])

    def test_wait_timeout_names_the_submitted_task(self) -> None:
        running = {"id": "task", "root_agent_id": "ag", "state": "running", "budget_tokens": 10,
                   "used_tokens": 0, "agents": []}
        responses = [(200, running), (200, {"events": [], "next_after": 0})] * 1000
        with server_with(*responses) as (url, _):
            with self.assertRaisesRegex(TaskWaitTimeout, "task task"):
                OwnerClient(url, "stub-owner").wait_for_task("task", timeout_s=0.05)
