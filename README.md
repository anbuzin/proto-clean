# Durable swarm prototype

One server owns tasks, history, memory, and sandbox copies. Workers lease agents
over HTTP and can be replaced without losing committed steps. The supervisor
restarts workers; its runner starts commands in separate shim processes. A safe
call can run again after a crash. A cut unsafe call waits for an owner decision.

## Layout

| Path | Responsibility |
|---|---|
| `swarm/wire.py` | Typed HTTP requests, replies, routes, and replay rules |
| `swarm/server/` | SQLite state, HTTP API, leases, calls, and sandbox copies |
| `swarm/server/deps.py`, `swarm/server/writes.py` | Handler dependencies, request replay, and agent write fence |
| `swarm/server/launches.py` | Launch grants, shim heartbeats, and exit reconciliation |
| `swarm/client/`, `swarm/owner/` | Typed clients and the owner CLI |
| `swarm/worker/steps.py` | Durable step writes shared by the model loop and Fx turns |
| `swarm/worker/` | Model loop, lease renewal, tools, and Fx turns |
| `swarm/supervisor/` | Worker pool, command runner, and launch shim |
| `tests/e2e/` | Real subprocess tests with offline models and OS signals |

## Run a live model

From this checkout, install the locked dependencies with Python 3.13 and `uv`:

```sh
uv sync --locked --python 3.13 --exclude-newer-package ai=2026-11-19
```

Terminal 1: start the server on loopback. Use a fresh state directory for this
schema; the prototype does not migrate an older database.

```sh
uv run --no-sync python -m swarm.server --state var/live --port 0
```

Terminal 2: set `AI_GATEWAY_API_KEY` in the environment and start a supervisor.
The server and supervisor do not import the model SDK; the worker reads the key
and removes it from its environment before running any task.

```sh
uv run --no-sync python -m swarm.supervisor --state var/live --workers 1
```

Terminal 3: submit a task. The default model is `openai/gpt-5-nano`.

```sh
uv run --no-sync python -m swarm.owner --state var/live submit --prompt 'Say hello.' --wait --timeout 60
```

Use `show TASK_ID`, `history AGENT_ID --after-seq N`, or `events --task TASK_ID`
to inspect state. `log PROCESS_ID` writes bytes; `--json` shows offsets and gaps.
A remote owner can replace `--state DIR` with `--url URL` and supply only
`SWARM_OWNER_TOKEN`. On restart, omit `--port` to reuse the saved port. Stop the
supervisor, then the server, with Ctrl-C; the state directory remains. Workers
claim only `loop` agents by default; use a worker command with `--kinds external`
to claim Fx turns on a host with Fx installed.

## Run without a key

In terminal 1, start the server for `var/offline`. In terminal 2, start the same
supervisor with the test model factory. In terminal 3, use the owner CLI as usual.

```sh
uv run --no-sync python -m swarm.server --state var/offline --port 0
```

```sh
SWARM_URL=$(uv run --no-sync python -c 'import json; print(json.load(open("var/offline/server.json"))["url"])')
uv run --no-sync python -m swarm.supervisor --state var/offline --workers 1 \
  --worker-command .venv/bin/python -m tests.fakes --server "$SWARM_URL"
```

```sh
uv run --no-sync python -m swarm.owner --state var/offline submit --prompt 'Say hello.' --wait
```

The root result is `Hello from the offline model.` No test fixture is in product code.

## Tests and limits

```sh
uv run --no-sync python -m unittest discover -s tests -t . -v
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design and [Limits](ARCHITECTURE.md#12-limits).
Read the limits before enabling command tools: the prototype does not confine commands running as your user.
