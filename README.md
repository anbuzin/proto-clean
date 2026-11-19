# Durable swarm prototype

One server owns task state, history, memory, and sandbox copies.
Workers lease agents over HTTP and can be replaced without losing committed steps.
The supervisor restarts workers; its runner starts commands in separate shim processes.
After a crash, safe tool calls can run again. A cut unsafe call waits for an owner decision.
The server and supervisor do not load the model library; only workers call the model.

## Layout

| Path | Responsibility |
|---|---|
| `swarm/wire.py` | Typed HTTP requests, replies, routes, and replay rules |
| `swarm/server/` | SQLite state, HTTP API, leases, calls, and sandbox copies |
| `swarm/client/` | Typed HTTP clients for the owner, worker, runner, and shim |
| `swarm/worker/` | Lease renewal, model loop, local tools, and external CLI turns |
| `swarm/supervisor/` | Worker pool, command runner, and launch shim |
| `swarm/owner/` | Owner CLI: submit, inspect, resolve, cancel, and manage processes |
| `tests/e2e/` | Real subprocess tests, with offline models and OS signals |

## Run an offline task

Use Python 3.13 and `uv`. Run each block from this checkout.

```sh
uv sync --locked --python 3.13 --exclude-newer-package ai=2026-11-19
```

Terminal 1: start the server. Port `0` selects a free loopback port. On restart,
omit `--port` to reuse the saved port.

```sh
uv run --no-sync python -m swarm.server --state var/hello --port 0
```

Terminal 2: start one worker and the command runner. This example uses the test
model so it needs no model key. A normal worker does not import the test code.

```sh
SWARM_URL=$(uv run --no-sync python -c 'import json; print(json.load(open("var/hello/server.json"))["url"])')
uv run --no-sync python -m swarm.supervisor --state var/hello --workers 1 \
  --worker-command .venv/bin/python -m tests.fakes --server "$SWARM_URL"
```

Terminal 3: submit a task and wait for its result. Output is JSON; the root agent
returns `Hello from the offline model.`

```sh
uv run --no-sync python -m swarm.owner --state var/hello submit --prompt 'Say hello.' --wait
```

The returned `id` identifies the task. Use `show TASK_ID`, `history AGENT_ID`, or
`events --task TASK_ID` in place of `submit …` to inspect committed state. Stop the
supervisor, then the server, with Ctrl-C. The `var/hello` directory retains state.

## Use a live model

Start the supervisor without `--worker-command`, with `AI_GATEWAY_API_KEY` in
its environment. The worker removes the key from its environment before making
model calls. Pass `--model MODEL_ID` to `submit` if the default Gateway model is
not suitable. To submit a full task specification, use `submit --spec FILE.json`.

```sh
uv run --no-sync python -m swarm.supervisor --state var/hello --workers 1
```

## Test and read more

```sh
uv run --no-sync python -m unittest discover -s tests -t . -v
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for flows and invariants. Read
[Limits](ARCHITECTURE.md#12-limits) before enabling command tools: the prototype
handles ordinary crashes but does not confine a command running as your user.
