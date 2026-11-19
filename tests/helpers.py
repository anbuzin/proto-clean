"""Run the public swarm entry points and coordinate tests through their APIs."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from swarm import wire
from swarm.client import OwnerClient, Unreachable


ROOT = Path(__file__).resolve().parents[1]


def matching_pids(*parts: str, parent: int | None = None) -> list[int]:
    """Find subprocesses by parent and command line using the OS process table."""
    rows = subprocess.check_output(["ps", "-axww", "-o", "pid=,ppid=,command="], text=True)
    matches = []
    for row in rows.splitlines():
        pid, ppid, *command = row.split(maxsplit=2)
        if command and (parent is None or int(ppid) == parent) and all(part in command[0] for part in parts):
            matches.append(int(pid))
    return matches


def send(pid: int, sig: signal.Signals) -> None:
    """Send a signal unless the process already exited."""
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def stop(process: subprocess.Popen) -> None:
    """Stop and reap a subprocess, even if its graceful shutdown stalls."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class SwarmTest(unittest.TestCase):
    """A swarm with a disposable state directory and no live model credentials."""

    def setUp(self) -> None:
        """Allocate state and arrange to include child logs in timeout diagnostics."""
        directory = tempfile.TemporaryDirectory(dir=ROOT, prefix=".swarm-e2e-")
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.script = self.state / "script.json"
        self.logs: list[Path] = []
        self.url = ""
        self.addCleanup(self._stop_stragglers)

    def _stop_stragglers(self) -> None:
        # A failed assertion can leave shims alive after the supervisor exits.
        if self.url:
            for shim in matching_pids("-m swarm.supervisor.shim", f"--server {self.url} "):
                for payload in matching_pids(parent=shim):
                    send(payload, signal.SIGKILL)
                send(shim, signal.SIGKILL)
        for worker in matching_pids("-m tests.fakes", str(self.script)):
            send(worker, signal.SIGKILL)

    def launch(self, *args: str) -> subprocess.Popen:
        """Launch a Python entry point; collect diagnostics and register cleanup."""
        log = self.state / f"child-{len(self.logs)}.log"
        self.logs.append(log)
        env = {key: val for key, val in os.environ.items() if key != "AI_GATEWAY_API_KEY"}
        with log.open("wb") as output:
            process = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env,
                                       stdout=output, stderr=subprocess.STDOUT)
        self.addCleanup(stop, process)
        return process

    def until[T](self, read: Callable[[], T], ready: Callable[[T], bool] = bool, *,
                 timeout: float = 35) -> T:
        """Read public state until a predicate holds, with bounded diagnostics."""
        end = time.monotonic() + timeout
        while True:
            value = read()
            if ready(value):
                return value
            if time.monotonic() >= end:
                logs = "\n".join(f"{log.name}: {log.read_text(errors='replace')[-3000:]}" for log in self.logs)
                self.fail(f"Timed out; last observation: {value!r}\n{logs}")
            time.sleep(0.05)

    def start_server(self, *, restart: bool = False, policy: Path | None = None) -> subprocess.Popen:
        """Start the server CLI and wait until the new PID answers health."""
        port = [] if restart else ["--port", "0"]
        options = ["--policy", str(policy)] if policy else []
        process = self.launch("-m", "swarm.server", "--state", str(self.state), *port, *options)

        def health() -> int | None:
            self.assertIsNone(process.poll(), "server exited during startup")
            try:
                server = json.loads((self.state / "server.json").read_text())
                tokens = json.loads((self.state / "tokens.json").read_text())
                self.url = server["url"]
                self.owner = OwnerClient(self.url, tokens["owner"])
                return self.owner.health().pid
            except (FileNotFoundError, Unreachable):
                return None

        self.until(health, lambda pid: pid == process.pid, timeout=10)
        return process

    def start_supervisor(self, workers: int = 1) -> subprocess.Popen:
        """Start the normal supervisor CLI with the fake worker command."""
        return self.launch("-m", "swarm.supervisor", "--state", str(self.state), "--workers", str(workers),
                           "--worker-command", sys.executable, "-m", "tests.fakes", "--script", str(self.script),
                           "--server", self.url, "--tools", "tests.fakes:TOOLS")

    def set_scripts(self, scripts: dict[str, list[Any]]) -> None:
        """Replace the fixture atomically so a worker only reads complete scripts."""
        temporary = self.script.with_suffix(".tmp")
        temporary.write_text(json.dumps(scripts))
        temporary.replace(self.script)

    def submit(self, prompt: str, *tools: str) -> str:
        """Submit an offline task through the owner client and return its task ID."""
        return self.owner.create_task(wire.TaskSpec(prompt=prompt, tools=list(tools))).task_id

    def root_agent(self, task_id: str) -> wire.AgentView:
        """Find the root agent in a public task view."""
        task = self.owner.get_task(task_id)
        return next(agent for agent in task.agents if agent.id == task.root_agent_id)

    def finished(self, task_id: str, result: wire.JSON = "done") -> wire.AgentView:
        """Require a successful root result and return the last root state."""
        agent = self.until(lambda: self.root_agent(task_id), lambda a: a.state in ("done", "failed"))
        self.assertEqual((agent.state, agent.result), ("done", result), agent)
        return agent

    def running_call(self, task_id: str, call_id: str, *, attempt: int = 1) -> wire.AgentView:
        """Wait for an attempt to receive a run grant for the named tool call."""
        return self.until(lambda: self.root_agent(task_id), lambda agent: any(
            call.call_id == call_id and call.state == "running" and call.attempt == attempt for call in agent.calls))

    def owner_cli(self, *args: str) -> dict[str, Any]:
        """Run the owner CLI and decode its public JSON response."""
        result = subprocess.run([sys.executable, "-m", "swarm.owner", "--state", str(self.state), *args],
                                cwd=ROOT, capture_output=True, text=True, timeout=65)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)
