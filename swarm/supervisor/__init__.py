"""The supervisor: keeps worker processes running and starts background commands.

Not an agent and has no job state. `pool` restarts workers; `runner` takes launch
orders from the server and starts one `shim` per launch. Workers and shims each run in
their own session, apart from the server and the supervisor.
"""
