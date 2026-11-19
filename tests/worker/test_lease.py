"""Use child processes to test the lease's hard-exit behavior without patching it."""

import subprocess
import sys
import unittest


CHILD = """
import sys
import time
from swarm import wire
from swarm.client import Rejected, Unreachable
from swarm.worker.lease import Lease

class Client:
    calls = 0
    def heartbeat(self, agent_id, attempt, timeout_s=1):
        self.calls += 1
        if self.calls == 1:
            return wire.HeartbeatReply(lease_until=0)
        if sys.argv[1] == "stale":
            raise Rejected(409, wire.ErrorReply(error="stale"))
        if sys.argv[1] == "late":
            time.sleep(timeout_s + 0.02)
            return wire.HeartbeatReply(lease_until=0)
        raise Unreachable("network unavailable")

claim = wire.Claim(agent_id="agent", task_id="task", attempt=1, lease_ttl_s=0.15,
                   spec=wire.TaskSpec(prompt="hello"), history=[], next_seq=0, pending_calls=[],
                   memory={}, sandbox_path="/sandbox")
lease = Lease(Client(), claim)
lease.start()
print("first heartbeat succeeded", flush=True)
time.sleep(3)
raise AssertionError("worker should have exited")
"""


class LeaseTest(unittest.TestCase):
    def test_stale_network_loss_and_late_success_stop_the_worker(self) -> None:
        for reason in ("stale", "network", "late"):
            with self.subTest(reason=reason):
                child = subprocess.run([sys.executable, "-c", CHILD, reason], capture_output=True, text=True, timeout=5)
                self.assertEqual(child.returncode, 3, child.stderr)
                self.assertEqual(child.stdout, "first heartbeat succeeded\n")
