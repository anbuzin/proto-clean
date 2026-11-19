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

    def test_i15_late_ack_cannot_extend_the_previous_deadline(self) -> None:
        script = """
from swarm import wire
from swarm.worker import lease as leases

clock = [0.0]
leases.monotonic = lambda: clock[0]

class Client:
    def heartbeat(self, agent_id, attempt, timeout_s):
        if clock[0] == 0.5:
            clock[0] = 1.01  # After the old deadline, but before a new one could end.
        return wire.HeartbeatReply(lease_until=0)

claim = wire.Claim(agent_id='agent', task_id='task', attempt=1, lease_ttl_s=1,
                   spec=wire.TaskSpec(prompt='test'), history=[], next_seq=0, pending_calls=[],
                   memory={}, sandbox_path='/sandbox')
lease = leases.Lease(Client(), claim)
lease.start()
lease.pause()
clock[0] = 0.5
lease.resume()
lease.close()
raise AssertionError('late reply extended the old deadline')
"""
        child = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=5)
        self.assertEqual(child.returncode, 3, child.stderr)
        self.assertIn("local deadline passed", child.stderr)
