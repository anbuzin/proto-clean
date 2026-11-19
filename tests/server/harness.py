"""Public HTTP proofs for the server; no worker or client library is needed."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from swarm import wire


class ServerHarness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.process = None
        self.log = tempfile.TemporaryFile(mode="w+")
        self.addCleanup(self.log.close)
        self.addCleanup(self.stop_server)
        self.start_server()

    def start_server(self, *, policy=None, cli=False, cli_port=None, reaper_interval=0.05):
        # The public configuration makes expiry tests fast without product test hooks.
        code = (
            "from pathlib import Path; from swarm.server.config import ServerConfig; "
            "from swarm.server.app import Server; import sys; "
            "c=ServerConfig(state_dir=Path(sys.argv[1]), port=0 if sys.argv[2]=='first' else None, "
            "policy_path=Path(sys.argv[3]) if sys.argv[3] else None, "
            "lease_ttl_s=1.5, reaper_interval_s=float(sys.argv[4]), backoff_base_s=0.05, "
            "backoff_max_s=0.1, grant_deadline_s=0.5, shim_silence_s=5); "
            "from threading import Event; s=Server(c); s.start(); Event().wait()"
        )
        first = not (self.state / "server.json").exists()
        if cli:
            command = [sys.executable, "-m", "swarm.server", "--state", str(self.state)]
            if cli_port is not None:
                command += ["--port", str(cli_port)]
        else:
            command = [sys.executable, "-c", code, str(self.state),
                       "first" if first else "restart", str(policy or ""), str(reaper_interval)]
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=self.log)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail(f"server exited before readiness: {self.process.returncode}")
            try:
                info = json.loads((self.state / "server.json").read_text())
                if info["pid"] == self.process.pid:
                    with urlopen(info["url"] + "/healthz", timeout=0.5) as response:
                        if json.load(response)["pid"] == self.process.pid:
                            self.url = info["url"]
                            self.tokens = json.loads((self.state / "tokens.json").read_text())
                            return
            except (OSError, ValueError, KeyError, URLError):
                pass
            time.sleep(0.05)
        self.fail("server did not become ready")

    def stop_server(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=5)
            self.process = None

    def request(self, name, body=None, *, token=None, expected=200, validate=True, **path):
        route = wire.ROUTE[name]
        if body is not None and validate:
            body = route.request.model_validate(body).model_dump(mode="json")
        data = None if route.method == "GET" else json.dumps(body).encode()
        auth = token if token is not None else self.tokens.get(route.auth)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + auth
        req = Request(self.url + route.path.format(**path), data=data, headers=headers, method=route.method)
        try:
            with urlopen(req, timeout=5) as response:
                status, raw = response.status, response.read()
        except HTTPError as error:
            status, raw = error.code, error.read()
        if status not in (expected if isinstance(expected, tuple) else (expected,)):
            self.log.flush()
            self.log.seek(0)
            self.fail(f"{name}: HTTP {status}: {raw!r}\nServer log:\n{self.log.read()}")
        if not raw:
            return None
        value = json.loads(raw)
        if status < 300:
            return route.reply.model_validate(value).model_dump(mode="json")
        return wire.ErrorReply.model_validate(value).model_dump(mode="json")

    def create(self, **spec):
        return self.request("create_task", {"request_id": self.id(), "spec": {"prompt": "test", **spec}})

    @staticmethod
    def id():
        return uuid4().hex

    def claim(self, request_id=None, worker="worker", expected=200):
        return self.request("claim", {"request_id": request_id or self.id(), "worker_id": worker,
                                      "pid": os.getpid(), "wait_s": 0}, expected=expected)

    def write(self, name, claim, **body):
        return self.request(name, {"attempt": claim["attempt"], "request_id": self.id(), **body},
                            agent_id=claim["agent_id"])

    def task(self, task_id):
        return self.request("get_task", task_id=task_id)

    def agent(self, task_id, agent_id):
        return next(a for a in self.task(task_id)["agents"] if a["id"] == agent_id)

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        self.fail("condition did not become true")

    def wait_agent(self, task_id, agent_id, state):
        return self.wait_for(lambda: (a if (a := self.agent(task_id, agent_id))["state"] == state else None))

    def user_step(self, claim):
        self.write("commit_step", claim, kind="user", expected_seq=0,
                   message={"role": "user", "content": "test"})

    def model_step(self, claim, calls, seq=1, usage=None):
        reply = self.write("admit_model_call", claim, step_seq=seq, estimated_tokens=10)
        self.assertTrue(reply["admitted"])
        return self.write("commit_step", claim, kind="model", expected_seq=seq,
                          message={"role": "assistant", "content": "reply"}, calls=calls, usage=usage)

    def call(self, name, claim, call_id="call", seq=1, **body):
        return self.request(name, {"attempt": claim["attempt"], "request_id": self.id(), **body},
                            agent_id=claim["agent_id"], step_seq=seq, call_id=call_id)

    def planned(self, tool, safety, args=None, call_id="call"):
        return {"call_id": call_id, "tool": tool, "safety": safety, "args": args or {}}

    def finish(self, claim, seq=2, call_ids=None):
        if call_ids is not None:
            self.write("commit_step", claim, kind="tools", expected_seq=seq,
                       message={"role": "tool", "content": "result"}, result_call_ids=call_ids)
            seq += 1
            self.model_step(claim, [], seq=seq)
        self.write("finish_agent", claim, status="done", result="ok")
