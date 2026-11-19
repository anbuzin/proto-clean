# ARCHITECTURE: a client-server agent swarm primitive

Status: plan and skeleton (planner, run 2, 2026-11-19). Source: `../proto/` (run 1), `../DESIGN.md` rev 8,
`../DESIGN-launch.md`, `../REPORT.md`. This file is the only brief for the builders. Real code exists only in
`swarm/wire.py` (the wire contract) and `swarm/server/schema.sql`. Each other module is a stub with its contract.

## 1. Terms

Each term has one meaning in code and text.

| Term | Meaning |
|---|---|
| server | The one process with no AI. It owns all state in SQLite and all files below `state_dir`. |
| owner | The human (or a script) on the owner API: submits tasks, reads state, resolves calls, cancels. |
| task | What the owner submits: a `TaskSpec` (prompt, model, tools, budget). It has one root agent. |
| agent | A record on the server: spec, history, memory, sandbox, parent, state. Not a process. One run per agent. |
| worker | A disposable process that runs one agent at a time under a lease. Holds only a cache of committed rows. |
| kind | How a worker runs an agent: `loop` (own loop on `ai.stream`) or `external` (a CLI turn through `ai.external`). |
| attempt | One try of an agent by one worker. Numbered 1, 2, ... per agent. The number is the fence. |
| lease | The right of the current attempt to write, until `lease_until`. Renewed by heartbeats. |
| fence | The check in each agent write: agent `leased`, same attempt number, lease not ended. Else `409 stale`. |
| step | One committed message: a model reply (with its planned calls), a tool message, or the first user message. |
| call | One tool call of a model reply, recorded with a state and a result. Key: (agent, step_seq, call_id). |
| safety | Class of a tool: `server` (server runs it), `idempotent` (safe to run again), `unsafe` (a cut run is `unknown`). |
| sandbox copy | The directory of one attempt: `sandboxes/<agent>/a<attempt>/`, cloned from the last copy. |
| strike | One count against an agent when an attempt is lost in a way that can be its fault. 3 strikes: `poison`. |
| process | A background command that an agent starts. The server keeps the record; a shim is its parent. |
| launch | One try to start a process. Its payload runs only after the server commits the grant. |
| supervisor | A process with no AI and no state. Its pool restarts workers; its runner starts shims. |
| runner | The part of the supervisor that takes launch orders and starts one shim per launch. |
| shim | The parent of one payload: holds it at a barrier, asks for the grant, writes the log, reports the exit. |
| policy | `CommandPolicy`: allows or refuses a command and gives its effect class (`local` or `external`). |

## 2. Invariants

These are the contract of the primitive. Each is a test target (section 9). The owner of the code is in brackets.

| # | Invariant |
|---|---|
| I1 | The server replies 2xx only after `COMMIT` returns. [store, http] |
| I2 | Fence: each agent write checks, after `BEGIN IMMEDIATE`, that the agent is `leased`, the attempt is current, and `lease_until > now`. A stale write changes nothing. [leases] |
| I3 | Each mutation except a heartbeat has a `request_id`. Its reply is stored in the same transaction. Same ID and body: the stored reply. Same ID, other body: `422`. [store] |
| I4 | A model reply and its planned calls commit in one step before any tool starts. Each call result commits before the next call or model call. The tool message commits only when all calls of its step are `done`. `expected_seq` must be the next seq. [steps, calls] |
| I5 | A call gets `run` at most once per attempt. A server tool runs inside `start_call`, in one transaction: exactly once. [calls] |
| I6 | When an attempt is lost or the server restarts, in the same transaction: each `running` `unsafe` call becomes `unknown`; `idempotent` calls stay `running` and get `run` again (with their checkpoint) in the next attempt. [leases] |
| I7 | Only the owner changes an `unknown` call (`resolve_call`). An agent with an `unknown` call is `blocked`. [calls] |
| I8 | Each attempt gets a new sandbox copy at a path that no other attempt gets. The claim reply comes only after the copy is `ready`. [sandboxes] |
| I9 | Waiting is in the database. A pending call (child, command) makes the worker yield; the agent is `waiting`. The transaction that fills the last pending call settles the agent to `ready`. No process waits for another agent. [steps, calls, tasks] |
| I10 | A lost attempt with no progress, or with a local call still `running`, adds a strike; otherwise strikes reset. Backoff `min(1 s * 2^(strikes-1), 60 s)`. At 3 strikes the agent is `failed` (`poison`), and its parent gets an error result. [leases] |
| I11 | A model call starts only after its admission commits within the task budget (all agents of the task). An admitted call with no usage counts at its estimate. [steps] |
| I12 | A payload runs only after its launch grant commits. A launch is granted at most once, never after its deadline or a revoke. A process has at most one active launch. [processes, shim] |
| I13 | An exit code comes only from the shim. With no shim report, the outcome is `unknown`; nothing is made up. [processes, shim] |
| I14 | The server does not import `ai`. It stores messages as opaque JSON. [server package] |
| I15 | A worker that cannot renew its lease before its local deadline stops itself (`os._exit`). [worker.lease] |
| I16 | A cut command (outcome `unknown`, or a stop it did not ask for) by its policy class: `local` gives the model `{outcome: "interrupted"}`; `external` makes the call `unknown` (I7). [processes] |
| I17 | `done` and `failed` are final. An agent's end fills its parent's call, stops its `agent` processes, and (root) ends the task. [tasks] |

Proof sketch (as run 1, `DESIGN.md` section 3): by I1 each acknowledged fact is in SQLite; clients read only the
server; a worker holds only a cache, code, and unacknowledged work; that work is a model call (safe to repeat) or a
`running` call (I6); by I2, I3, I8 a late or retried worker cannot change a committed fact or a file that a new
attempt reads. Thus `kill -9` of every worker loses only unacknowledged work, and the server knows which.

## 3. Components

```
 owner: python -m swarm.owner / curl        (HTTP, owner token)
            |
            v
 +---------------------------------------------------------------+
 | server  (python -m swarm.server, own session, no ai)          |
 |   http.py   routes from wire.ROUTES, auth, JSON <-> wire      |
 |   domain    tasks  leases  steps  calls  sandboxes  processes |
 |             policy (CommandPolicy)                            |
 |   store.py  SQLite WAL (schema.sql)    reaper thread 0.5 s    |
 |   files     state/sandboxes/<agent>/a<N>/  state/processes/   |
 +------^------------------------------^--------------------^----+
        | agent API (worker token)     | runner API          | launch API (launch secret)
        |                              | (runner token)      |
 +------+-------------+     +----------+-----------------+   |
 | worker (1 per slot)|     | supervisor (own session)   |   |
 |  lease  loop/ext.  |<----|  pool: start/restart       |   |
 |  tools  models     |spawn|  runner: next_launch ->    |   |
 +--------------------+     |          spawn shim -------+---+--> shim (own session) -> payload (own group)
   own session each         +----------------------------+          log file below state/processes/
   all clients use swarm.client; all share swarm.wire
```

## 4. Layout

```
proto-clean/
  ARCHITECTURE.md  README.md (part D)  pyproject.toml  uv.lock  .gitignore
  swarm/
    __init__.py           package overview
    wire.py               REAL. The contract: typed requests, replies, views, ROUTES, server tool args
    server/
      __main__.py         python -m swarm.server --state DIR --port 0; writes server.json
      app.py              Server: lock, tokens, startup recovery order, reaper thread, HTTP start
      config.py           ServerConfig (TTL, strikes, backoff, launch limits, paths)
      http.py             route matching, auth, validation, error mapping; no domain logic
      store.py            Store, Tx, ApiError, idempotent replies, events, change wake-ups
      schema.sql          REAL. 11 tables
      tasks.py            tasks, hierarchy, cancel, history, events, run_child, memory tools, terminal states
      leases.py           claim (long poll), heartbeat, fence, end_attempt, strikes, settle, expiry, recovery
      steps.py            model-call admission and budget, commit_step, yield, finish
      calls.py            start_call (run grant, server tool dispatch), checkpoint, finish, owner resolve
      sandboxes.py        copy per attempt: reserve, clone (APFS clonefile), ready, recover
      processes.py        process records, launches, grant, shim heartbeat, exit, logs, process tools
      policy.py           CommandPolicy protocol, AllowList, default_policy
    client/
      api.py              HttpClient (retries, same request_id), OwnerClient, AgentClient, RunnerClient, LaunchClient
    worker/
      __main__.py         python -m swarm.worker --server URL [--model-factory M:F] [--tools M:A]; claim loop
      lease.py            Lease: first heartbeat, renewal thread, local deadline, os._exit
      loop.py             kind loop: the step loop on ai.stream + ToolRunner
      tools.py            Registry, ToolEntry, CallContext, local file tools, server tool stubs
      models.py           ModelFactory seam, gateway_model, estimate_tokens
      external.py         OPTIONAL. kind external: one Fx turn as the call cli_turn
    owner/
      __main__.py         python -m swarm.owner --state DIR submit|show|history|events|resolve|cancel|procs|log|stop
    supervisor/
      __main__.py         python -m swarm.supervisor --state DIR --workers N [-- worker args]
      pool.py             WorkerPool: N slots, own sessions, restart backoff
      runner.py           Runner: next_launch long poll, start shim, secret on stdin
      shim.py             barrier, grant, release, log with cap, heartbeat, stop, exit report
  tests/
    server/  worker/  supervisor/  e2e/   fakes.py  helpers.py   (by part, section 10)
```

State directory: `state.db`, `tokens.json` (0600: `owner`, `worker`, `runner`), `server.json` (`url`, `pid`),
`server.lock`, `supervisor.lock`, `sandboxes/`, `processes/<process>/<launch>.log`.

## 5. API

All bodies JSON; models in `swarm/wire.py`; the table is `wire.ROUTES`. Errors: `wire.ErrorReply` with
`401 unauthorized`, `404 not_found`, `409 stale|conflict`, `422 invalid`, `503 busy`, `500 internal`.

| Route | Method, path | Auth | Request -> reply |
|---|---|---|---|
| health | GET /healthz | none | - -> Health |
| create_task | POST /v1/tasks | owner | CreateTask -> TaskCreated |
| get_task | GET /v1/tasks/{task_id} | owner | - -> TaskView (agents, attempts, calls, memory) |
| cancel_task | POST /v1/tasks/{task_id}/cancel | owner | OwnerWrite -> TaskView |
| get_history | GET /v1/agents/{agent_id}/history | owner | ?after_seq -> HistoryPage |
| list_events | GET /v1/events | owner | ?task_id&after&wait_s -> EventsPage (long poll) |
| resolve_call | POST /v1/agents/{a}/calls/{seq}/{call}/resolve | owner | ResolveCall -> CallView |
| list_processes | GET /v1/tasks/{task_id}/processes | owner | - -> ProcessList |
| read_log | GET /v1/processes/{p}/log | owner | ?offset&max_bytes -> LogChunk |
| stop_process | POST /v1/processes/{p}/stop | owner | OwnerWrite -> ProcessView |
| claim | POST /v1/claims | worker | ClaimRequest -> Claim, or 204 after wait_s |
| heartbeat | POST /v1/agents/{a}/heartbeat | worker | Heartbeat -> HeartbeatReply |
| admit_model_call | POST /v1/agents/{a}/model-calls | worker | ModelCallRequest -> ModelCallReply |
| commit_step | POST /v1/agents/{a}/steps | worker | StepRequest -> StepReply |
| start_call | POST /v1/agents/{a}/calls/{seq}/{call}/start | worker | AgentWrite -> CallStartReply (run, use, wait, blocked, stop) |
| checkpoint_call | POST .../calls/{seq}/{call}/checkpoint | worker | CallCheckpoint -> Ack |
| finish_call | POST .../calls/{seq}/{call}/finish | worker | CallFinish -> CallView |
| yield_agent | POST /v1/agents/{a}/yield | worker | AgentWrite -> YieldReply (continue, released) |
| finish_agent | POST /v1/agents/{a}/finish | worker | FinishRequest -> AgentStateReply |
| next_launch | POST /v1/runner/launches | runner | NextLaunchRequest -> LaunchOrder, or 204 |
| grant_launch | POST /v1/launches/{l}/grant | launch | GrantRequest -> GrantReply |
| launch_heartbeat | POST /v1/launches/{l}/heartbeat | launch | LaunchHeartbeat -> LaunchHeartbeatReply (stop?) |
| report_exit | POST /v1/launches/{l}/exit | launch | ExitReport -> Ack |

Server tools (run inside `start_call`, args in `wire.SERVER_TOOL_ARGS`): `memory_get`, `memory_set`,
`run_child(prompt, tools?)` (wait), `run_command(argv, timeout_s)` (wait, barrier), `process_start(argv, lifetime)`,
`process_read(process_id, offset, max_bytes)`, `process_stop(process_id)`, `process_list()`.
Local tools (worker): `read_file`, `write_file`, `list_files`, plus any `--tools` entries.

## 6. Schema

`swarm/server/schema.sql` is the source; summary: `tasks` (state, budget) -> `agents` (tree by `parent_id`; state,
attempt, lease, strikes, result) -> `attempts` (worker, pid, end reason, progress, sandbox copy) ; `messages`
(agent, seq, opaque JSON) ; `calls` (agent, step_seq, call_id; safety, state, checkpoint, result, child, process) ;
`memory` (agent, key, version) ; `model_calls` (admission, estimate, usage) ; `requests` (stored replies) ;
`events` (append-only log) ; `processes` (argv, cwd, class, lifetime, desired, state, outcome) -> `launches`
(secret hash, grant deadline, pid, heartbeat). Agent states: `ready -> leased -> (waiting | blocked | ready) -> done | failed`.

## 7. Flows

Run a task.
1. Owner `create_task(spec)`: task, root agent `ready`.
2. A worker `claim`s: attempt 1, lease, sandbox copy `a1` cloned from `spec.workspace`. One heartbeat, then work.
3. History empty: commit seq 0 = `user_message(prompt)`. `admit_model_call`; `ai.stream`; commit the reply with
   its planned calls (I4).
4. For each call: `start_call` -> `run` (local: `ToolRunner`, then `finish_call`) or `use` (server tool result).
5. All calls done: commit the tool message; back to 3. A reply with no calls: `finish_agent(done)`; task `done`.

Crash and resume.
1. `kill -9` of the worker during step 4 (call `running`, attempt 1). Heartbeats stop.
2. Reaper: `lease_until` passed -> attempt `lost`; `unsafe` running calls -> `unknown` (I6); strike if no progress
   or a local call was running (I10); `settle` -> `ready` (after backoff) or `blocked`.
3. The supervisor's pool sees the exit and starts a new worker. It claims attempt 2; copy `a2` cloned from `a1`.
4. The claim gives the history and the pending calls. `done` calls: `use`. The `idempotent` running call: `run`
   again with its checkpoint. A write from attempt 1, if it comes back: `409 stale` (I2), and it can only touch `a1`.
5. Server crash instead: workers retry to their local deadline and exit (I15); a new server run ends all attempts
   `server_restart` (no strike), then listens.

Child agents.
1. The parent's reply has `run_child(prompt)` twice. Each `start_call` creates a child agent (`ready`, depth+1,
   seeded from the parent copy) and sets the call `waiting` in one transaction (I5, I9).
2. The parent worker yields: attempt ends `yielded`, parent `waiting`. Its worker is free for the children.
3. Each child runs as in "run a task". Its end fills the parent's call (`done`, `{agent_id, status, result}`) (I17).
4. The last fill settles the parent to `ready`; a worker claims attempt N+1 and commits the tool message.
   A poisoned child gives the parent an error result, not a hang.

A background process.
1. `process_start(argv)` (or `run_command`): `policy.check` -> process `requested`, cwd = this attempt's copy.
2. Runner `next_launch`: process `launching`, launch `issued` with a secret; the runner starts a shim (new session),
   order on stdin.
3. Shim forks the payload, which waits at the barrier; `grant(pid)` commits; shim sends `G`; payload `exec`s (I12).
4. Shim writes the log (cap), heartbeats each 1 s; `process_read` returns exact bytes and `next_offset`.
5. All workers die: the payload and shim live on; a new attempt reads from the committed offset.
6. Exit: `report_exit` -> process `ended`; a `run_command` call is filled and its agent wakes. Shim dead: after
   10 s of silence the outcome is `unknown` (I13), then I16 for `run_command`.

## 8. Mechanisms of `proto/`: kept, simpler, dropped

A production build adds back the "dropped" rows and the removed parts of the "simpler" rows.

| Mechanism | proto file | Verdict | Reason |
|---|---|---|---|
| Lease, attempt fence, reaper expiry | server/db.py, reaper.py | kept (I2) | Core of crash safety. |
| Request IDs with stored replies | server/db.py | kept (I3) | A lost reply must not give a second step or claim. |
| Step before action, call results before next action | agent/worker.py, server/app.py | kept (I4) | The unit of durability. |
| Run grant once per attempt; server tools in-transaction | server/app.py start_effect | kept (I5) | Exactly-once for server effects. |
| Unsafe -> unknown -> owner resolve | server/reaper.py, app.py | kept (I6, I7) | No proof that a cut unsafe tool did nothing. |
| Workspace copy per attempt (W18) | server/tools.py versions | simpler (I8) | Same rule; automatic at claim, no `sandbox_open`, no CLI-home copy. |
| Wait for children in the DB (W10) | server/tools.py run_child, db.py | kept (I9) | Owner requirement. |
| Strikes, backoff, poison | server/reaper.py, db.py | kept (I10) | Bounds a job that kills its worker every time. |
| Probation (struck jobs one at a time) | server/app.py claim, attempts.probation | dropped | Finds the culprit of a repeated tree kill; poison already bounds the cost. |
| Worker local deadline, `os._exit` | agent/worker.py Lease | kept (I15) | A live old worker stops before its lease ends on the server. |
| Server run ID, monotonic lease clock | server/db.py | simpler | Wall clock; startup ends every lease, so old deadlines are never compared. |
| One server per state dir (lock file) | server/app.py | kept | Two servers would end each other's leases. |
| Long-poll disconnect check | server/app.py Handler | dropped | A lease to a dead worker costs one TTL; size from a fault proof. |
| Code version pin on claim | server/tools.py code_version | dropped | One code tree per deploy; production adds a version filter. |
| Model-call admission and budget | server/live.py | simpler (I11) | Per task, children included (fixes the run-1 gap); no provider error taxonomy. |
| Delegation limits | server/live.py | kept | Spec fields `max_children`, `max_depth`. |
| Secret scans of messages and tool results | agent/live.py contains_secret | dropped | The key is never in the model context or a tool env; production adds redaction. |
| Launch barrier before exec | proc_shim.py | simpler (I12) | One barrier pipe; no exec-status pipe, no `phase.json` evidence. |
| Single-use launch secret on stdin | proc_runner.py, server/proc_launch.py | kept | A shim can act only on its own launch. |
| Runner epoch | server/proc_launch.py | dropped | Issue is one transaction with a deadline; a lost order expires and retries. |
| Reconciliation table, receipt import from disk | proc_runner.py, proc_launch.py | simpler | Shim silence for 10 s -> `unknown` (I13). |
| Process identity checks (libproc birth, boot ID) | proc_identity.py | dropped | Only the shim signals, and only its own unreaped child; orphans after a shim death are a stated limit. |
| Signals table (send once), leftover-group, unverified flags | server/proc_launch.py | dropped | Follow from the identity checks above. |
| Log cap at the writer; exact-byte reads; gaps | proc_shim.py, server/proc_log.py | kept | Bounded disk; no lost or split bytes. |
| Cut-command rule by class | server/proc_effects.py | kept (I16) | Class now comes from the policy, not a recipe. |
| Fixed recipes | proc_recipe.py | replaced | Real argv behind `CommandPolicy` (allow list). |
| Fx cut policy (block, trusted-local) | agent/external.py, server/external.py | simpler | It is the safety of one `cli_turn` call: `unsafe` or `idempotent`. |
| Fx native store reading, marker, rebind | agent/fx_native.py | dropped | Fx-version-specific; a cut turn blocks or resumes instead. |
| Fx launcher identity record | agent/fx_launcher.py | dropped | Correctness comes from the sandbox copy (I8). |
| MCP bridge for server tools in Fx | agent/external.py bridge | dropped | Optional later; the CLI uses its own tools only. |
| Memory gate and footprint monitor | DESIGN.md section 2 | dropped | Never built in run 1; next production step. |
| Orphan watch, setsid launcher, signal forwarding | supervisor.py | simpler | Workers start in their own sessions; nothing more. |
| Simulated OOM, `PROTO_*GATE*` hooks, fixture scenarios and role tables, fixture endpoints, demos, launchd, commit ledgers | agent/tools.py, agent/scenarios.py, worker.py, demo-*.sh, launchd/ | dropped | Test and demo code; no hook is in product code. Tests use the model seam and `--tools`. |

## 9. Decisions (brief items 2 to 5)

- In the wild: the owner submits a real `TaskSpec` (prompt, Gateway model ID, tool names, workspace path, budget).
  Commands are real argv checked by `CommandPolicy`. The model comes from a `ModelFactory`
  (`--model-factory module:func`); tests pass one that returns `ai.testing.FakeModel`, and add test tools with
  `--tools module:attr`. Reason: both are normal extension points, so no scenario code is in the product.
- Names changed from run 1: job -> merged into agent (one run per agent, so one fewer table and ID); gen ->
  attempt; effect -> call; version -> sandbox copy; proc -> process; shell_run -> run_command; kind native -> loop;
  sandbox_open -> automatic at claim. Reason: one plain word per thing, and the code reads like the flows.
- Stack: Python 3.13, uv, `ai==0.10.0`; HTTP on the standard library (`http.server.ThreadingHTTPServer`,
  `urllib.request`); typed contract on pydantic, which `ai` already locks. Reason: no new dependency, the lock has
  the same 15 packages as `proto/`, and a reader sees the whole request path in two short files.
- Size: about 3,050 lines of logic (parts S, C, P in section 11) plus `wire.py` (485 lines, mostly declarations).
  This is about 15% above the 3,000 aim; the process protocol (shim, launches, logs: about 650) is the largest share.
  Without the optional `external.py` (about 180) it is about 2,870 plus `wire.py`. Each file stays below 300 lines,
  except `wire.py`: one file on purpose, so that the whole contract is in one place.

## 10. Tests

Each test uses public interfaces only: HTTP, the client library, the CLIs, and process signals. Servers, supervisors,
and workers run as real subprocesses on a temporary state directory. Suite: `unittest`. Part in brackets.

| # | Test | Shows |
|---|---|---|
| T1 | e2e with `FakeModel`: write_file, memory_set, final text -> task `done`; history, memory, file in the last copy [D] | Run a task |
| T2 | Kill the worker (`kill -9` by the attempt pid) during an idempotent test tool; the pool restarts it; task `done`; the call has `runs == 2`; no step twice [D] | Crash and resume |
| T3 | Kill the supervisor and every worker; owner API still shows all rows; start a new supervisor; task `done` [D] | State survives |
| T4 | Stale attempt: attempt 1 writes after attempt 2 claimed -> 409, no row changed [S] | I2 |
| T5 | Same `request_id` and body -> same reply, one row; other body -> 422 [S] | I3 |
| T6 | Lease lost during an unsafe call -> agent `blocked`; owner `resolve_call`; next attempt finishes [S, D] | I6, I7 |
| T7 | Three lost attempts with no progress -> `failed` `poison`; the parent's call gets an error result [S] | I10 |
| T8 | Parent with two `run_child` calls: parent yields, children run, parent wakes and finishes; tree in `get_task` [D] | I9 |
| T9 | Copy per attempt: attempt 2 path differs, has attempt 1 files; a write to the old path is not seen [D] | I8 |
| T10 | `process_start`, kill all workers, process lives, new attempt reads from the saved offset, `process_stop` -> `signaled` [D] | Background process |
| T11 | `run_command(["python3","-c",...])`: agent `waiting`, then exit code and output in the call result [D] | I9, process |
| T12 | Policy refuses a program: error result to the model, no launch [S] | Policy |
| T13 | Kill the shim during `run_command`: `unknown`; `local` -> interrupted result; `external` -> agent `blocked` [D] | I13, I16 |
| T14 | Budget too small: admission refused, agent `failed` `budget`, no model call made [S] | I11 |
| T15 | Restart the server while a worker holds a lease: attempt `server_restart`, strikes 0, task `done` [D] | Server crash |
| T16 | A launch: one grant; a second grant with another pid refused; no grant after the deadline; startup revokes `issued` [S, P] | I12 |
| T17 | `import swarm.server` and all its modules in a fresh interpreter: `ai` not in `sys.modules` [S] | I14 |
| T18 | Cancel: all agents `failed` `cancelled`, processes stopped, the old worker's next write -> 409 [S] | I17 |
| T19 | Optional, skipped with no key or no `fx`: live Gateway run with a kill; live Fx turn with `on_cut=resume` [D] | Live path |

## 11. Build parts

Three builders work in parallel on disjoint paths, then one integrates. Shared and frozen: `swarm/wire.py`,
`swarm/server/schema.sql`, the stub signatures. A builder that needs a change to a frozen file posts it in its Board
thread; the integration part applies it. Each part commits with `SWARM=1 git commit` on its own paths only.
Commands run in `proto-clean/` after `uv sync --locked --python 3.13 --exclude-newer-package ai=2026-11-19`.

| Part | Owns | Must meet | Size (lines) | Proof command |
|---|---|---|---|---|
| S: server | `swarm/server/**` (not schema.sql), `tests/server/**` | Every route of `wire.ROUTES`; I1 to I14, I16, I17; startup order in `app.py`; `python -m swarm.server --state DIR --port 0` writes `server.json`; `tokens.json` keys `owner`, `worker`, `runner` | about 1,450 (store 150, http 170, app 110, tasks 220, leases 220, steps 150, calls 200, sandboxes 90, processes 280, policy 50, config+main 60) | `uv run --no-sync python -m unittest discover -s tests/server -t . -v` (T4, T5, T6 server side, T7, T12, T14, T16 server side, T17, T18; tests use `urllib` and `swarm.wire` only) |
| C: clients | `swarm/client/**`, `swarm/worker/**`, `swarm/owner/**`, `tests/worker/**` | The client signatures in `client/api.py`; the loop in `worker/loop.py` docstring; I4, I15 on the worker side; `--model-factory`, `--tools`; token from `SWARM_WORKER_TOKEN` | about 1,150 (client 200, lease 100, loop 230, tools 170, models 60, worker main 70, owner 130, external 180 optional) | `uv run --no-sync python -m unittest discover -s tests/worker -t . -v`: the loop with `FakeModel` against an in-memory `AgentClient` stand-in (same methods); a resume from a history cut at each step; the client retries with the same `request_id` against a stdlib stub server |
| P: processes | `swarm/supervisor/**`, `tests/supervisor/**` | Shim steps 1 to 6 in `shim.py`; secret on stdin only; payload env from the order only; pool restart and backoff; no import of `ai` | about 450 (pool 120, runner 90, shim 200, main 50) | `uv run --no-sync python -m unittest discover -s tests/supervisor -t . -v`: shim with a stand-in `LaunchClient`: denied -> payload never runs; granted -> exit code and log bytes; stop -> TERM then KILL; log cap; pool restarts a dying command |
| D: integration | `tests/e2e/**`, `tests/fakes.py`, `tests/helpers.py`, `README.md`; seam fixes anywhere, each listed in its report | T1 to T19 pass; README with run commands | about 700 tests, 80 README | `uv run --no-sync python -m unittest discover -s tests -t . -v` exit 0, then `pgrep -fl 'swarm\.(server|worker|supervisor)'` prints nothing |

Order: S, C, P start at once (C writes `client/api.py` first, because P's real runs and D need it). D starts after
all three report. Tests must clean up every process that they start (kill the sessions in `tearDown`).

## 12. Not verified by the planner

- No module except `wire.py` has a body; the sizes are estimates from the proto modules that they replace.
- `ai.external.Fx` with `resume=` in a new sandbox copy is taken from run 1 (`REPORT.md` section 4), not tried here.
- APFS `clonefile` through ctypes: run 1 used it (`proto/server/tools.py`); not tried in this skeleton.
- `FakeModel` with several scripts across a process restart: run 1 used one script per fixture with fixed call IDs.
