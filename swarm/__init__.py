"""A client-server agent swarm primitive.

The server (`swarm.server`) has no AI and owns all state. The owner and the agents are
HTTP clients (`swarm.client`). A worker process (`swarm.worker`) runs one agent at a
time and can die at any moment; the supervisor (`swarm.supervisor`) starts workers
again and starts background commands through shims. `swarm.wire` is the contract
that all of them share. See ARCHITECTURE.md.
"""
