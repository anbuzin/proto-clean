"""Test the launch barrier, shim process lifecycle, and worker restarts."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event

from swarm import wire
from swarm.client import Unreachable
from swarm.supervisor.pool import WorkerPool
from swarm.supervisor.runner import Runner


class ShimTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def run_shim(self, script: str, mode: str = "grant", cap: int = 2**22) -> wire.ExitReport:
        home = self.directory / "home"
        order = wire.LaunchOrder(
            launch_id="test-launch", process_id="test-process", secret="test-secret",
            argv=[sys.executable, "-c", script], cwd=str(self.directory),
            env={"HOME": str(home), "PAYLOAD_VALUE": "approved"}, private_dirs=[str(home)],
            log_path=str(self.directory / "log"), log_cap_bytes=cap, grant_deadline_s=5,
        )
        with subprocess.Popen(
            [sys.executable, "-m", "tests.supervisor.shim_case", mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, env=os.environ | {"SHIM_ONLY_VALUE": "do-not-inherit"},
        ) as shim:
            try:
                _, stderr = shim.communicate(order.model_dump_json().encode(), timeout=10)
            finally:
                if shim.poll() != 0:
                    if shim.poll() is None:
                        shim.kill()
                        shim.wait()
                    pid_file = self.directory / "pid"
                    if pid_file.exists():
                        try:
                            os.killpg(int(pid_file.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
            self.assertEqual(shim.returncode, 0, stderr.decode())
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        return wire.ExitReport.model_validate_json((self.directory / "reported.json").read_bytes())

    def test_denied_payload_does_not_run_or_replace_receipt(self) -> None:
        receipt = self.directory / "exit.json"
        receipt.write_bytes(b"existing receipt")
        report = self.run_shim("open('ran', 'w').close()", mode="deny")
        self.assertFalse(report.started)
        self.assertFalse((self.directory / "ran").exists())
        self.assertFalse((self.directory / "log").exists())
        self.assertEqual(receipt.read_bytes(), b"existing receipt")

    def test_granted_payload_logs_bytes_and_saves_exit_code(self) -> None:
        report = self.run_shim("""
import os, sys
assert not os.read(0, 1)
assert os.environ['PAYLOAD_VALUE'] == 'approved'
assert 'SHIM_ONLY_VALUE' not in os.environ
open('ran', 'w').close()
os.write(1, b'out\\x00')
os.write(2, b'err\\xff')
sys.exit(7)
""")
        self.assertTrue((self.directory / "ran").exists())
        self.assertEqual((self.directory / "log").read_bytes(), b"out\0err\xff")
        self.assertEqual(report, wire.ExitReport(exit_code=7, log_bytes=8, output_tail="out\0err\ufffd"))

    def test_log_cap_counts_dropped_bytes_and_tails_the_log(self) -> None:
        report = self.run_shim("import sys; sys.stdout.buffer.write(b'A' * 1000 + b'B' * 70000)", cap=5000)
        self.assertEqual((self.directory / "log").read_bytes(), b"A" * 1000 + b"B" * 4000)
        self.assertEqual(report, wire.ExitReport(
            exit_code=0, log_bytes=5000, truncated_bytes=66000, output_tail="A" * 96 + "B" * 4000,
        ))

    def test_stop_sends_term_before_kill(self) -> None:
        report = self.run_shim("""
import os, signal
signal.signal(signal.SIGTERM, lambda *_: os.write(1, b'TERM'))
while True:
    signal.pause()
""", mode="stop")
        self.assertEqual(report, wire.ExitReport(signal=signal.SIGKILL, log_bytes=4, output_tail="TERM"))

    def test_heartbeat_failure_does_not_stop_draining(self) -> None:
        report = self.run_shim("""
import sys, time
for _ in range(12):
    sys.stdout.buffer.write(b'X' * 65536)
    sys.stdout.buffer.flush()
    time.sleep(0.2)
""", mode="outage")
        self.assertTrue((self.directory / "heartbeats").read_text().splitlines())
        self.assertEqual((self.directory / "log").read_bytes(), b"X" * (12 * 65536))
        self.assertEqual(report.exit_code, 0)


class PoolTests(unittest.TestCase):
    def test_restarts_a_worker_after_a_quick_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            starts = Path(directory) / "starts"
            command = [sys.executable, "-c", """
import os, sys, time
from pathlib import Path
assert os.getpid() == os.getsid(0)
path = Path(sys.argv[1])
restart = path.exists()
with path.open('a') as file:
    file.write(f'{os.getpid()} {time.monotonic()}\\n')
if restart:
    time.sleep(30)
""", str(starts)]
            pool = WorkerPool(command, 1, dict(os.environ))
            deadline = time.monotonic() + 5
            try:
                while time.monotonic() < deadline:
                    pool.tick(time.monotonic())
                    if starts.exists() and len(rows := starts.read_text().splitlines()) >= 2:
                        break
                    time.sleep(0.02)
                else:
                    self.fail("worker did not restart")
                (first, began), (second, restarted) = (row.split() for row in rows)
                self.assertNotEqual(first, second)
                self.assertGreaterEqual(float(restarted) - float(began), 0.49)
                self.assertEqual(pool.pids(), [int(second)])
            finally:
                pool.close()
            self.assertEqual(pool.pids(), [])


class RunnerTests(unittest.TestCase):
    def test_transport_failure_backs_off_before_fresh_poll(self) -> None:
        stop = Event()
        calls: list[float] = []

        class StandIn:
            def next_launch(self, runner_id: str, wait_s: float) -> None:
                calls.append(time.monotonic())
                if len(calls) == 1:
                    raise Unreachable()
                stop.set()

        runner = Runner(StandIn(), "unused", "runner")
        with self.assertLogs("swarm.supervisor.runner", level="WARNING"):
            runner.run(stop)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(calls[1] - calls[0], 0.5)


class SupervisorTests(unittest.TestCase):
    def test_lock_and_worker_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "server.json").write_text('{"url":"unused"}')
            (state / "tokens.json").write_text('{"worker":"test-token"}')
            pid_path = state / "worker.pid"
            worker = """
import os, sys, time
from pathlib import Path
assert os.environ['SWARM_WORKER_TOKEN'] == 'test-token'
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(30)
"""
            command = [sys.executable, "-m", "swarm.supervisor", "--state", directory,
                       "--no-runner", "--worker-command", sys.executable, "-c", worker, str(pid_path)]
            with subprocess.Popen(command, start_new_session=True) as supervisor:
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and not pid_path.exists():
                        time.sleep(0.02)
                    self.assertTrue(pid_path.exists(), "worker did not start")
                    duplicate = subprocess.run(command, capture_output=True, timeout=3)
                    self.assertEqual(duplicate.returncode, 2)
                    self.assertIn(b"already holds", duplicate.stderr)
                finally:
                    supervisor.terminate()
                    try:
                        supervisor.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        supervisor.kill()
                        supervisor.wait()
                    if pid_path.exists():
                        pid = int(pid_path.read_text())
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            pass
                        else:
                            os.kill(pid, signal.SIGKILL)
                            self.fail("worker stayed alive after supervisor stopped")
                self.assertEqual(supervisor.returncode, 0)
