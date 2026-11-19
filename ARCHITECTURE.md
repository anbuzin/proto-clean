# ARCHITECTURE: a client-server agent swarm primitive

Status: plan and skeleton, revision 3, final for the builders (planner, run 2, 2026-11-19), after the checks
`../run2/checks/plan-check.md` and `plan-recheck.md` (replies: `../run2/checks/plan-fix.md`, `plan-fix2.md`). Sources: `../proto/` (run 1), `../DESIGN.md` rev 8, `../DESIGN-launch.md`.
This file is the only brief for the builders. Real code: `swarm/wire.py` (the contract) and `swarm/server/schema.sql`.
Each other module is a stub with its contract. The aim is a clean prototype for the normal crash (`kill -9` of
workers, of the supervisor, of the server), not protection against hostile same-user code (section 12, "Limits").

## 1. Terms

| Term | Meaning |
|---|---|
| server | The one process with no AI. It owns all state in SQLite and all files below `state_dir`. |
| owner | The human or script on the owner API: submits tasks, reads state, resolves tool calls, cancels. |
| task | A `TaskSpec` (prompt, model, tools, budget) that the owner submits. It has one root agent. |
| agent | A record on the server: spec, history, memory, sandbox copies, parent, state. Not a process. |
| worker | A disposable process that runs one agent at a time under a lease. It holds only a cache. |
| kind | How a worker runs an agent: `loop` (own loop on `ai.stream`) or `external` (one Fx CLI turn). |
| attempt | One try of an agent by one worker, numbered 1, 2, ... The number is the fence. |
| lease | The right of the current attempt to write, until `lease_until` (server monotonic clock). |
| fence | The check in each agent write: agent `leased`, same attempt, lease not ended. Else `409 stale`. |
| step | One committed message of kind `user` (seq 0), `model` (a model reply and its tool calls), or `tools` (the tool message). |
| pending step | The last step, when it is a `model` step with tool calls. |
| model call | One `ai.stream` request. It must be admitted (budget) before it starts. |
| tool call | One tool call of a model reply, with a state and a result. Key: (agent, step_seq, call_id). |
| safety | Class of a tool: `server` (the server runs it), `idempotent` (safe to run again), `unsafe` (a cut run is `unknown`). |
| sandbox copy | The directory of one attempt, `sandboxes/<agent>/a<attempt>/`, cloned from a stable source. |
| strike | A count against an agent when an activated attempt is lost in a way that can be its fault. |
| process | A background command that an agent starts. The server keeps the record; a shim is its parent. |
| launch | One try to start a process. Its payload runs only after the server commits the grant. |
| supervisor | A process with no AI and no state: its pool restarts workers, its runner starts shims. |
| shim | The parent of one payload: holds it at a barrier, asks for the grant, writes the log, reports the exit. |
| policy | `CommandPolicy`: allows or refuses a command, gives its environment and effect class (`local`, `external`). |

## 2. Invariants

Each is a test target (section 10). The module that owns it is in brackets.

| # | Invariant |
|---|---|
| I1 | The server replies 2xx only after `COMMIT` returns. [store, http] |
| I2 | Fence: each agent write checks, after `BEGIN IMMEDIATE`, that the agent is `leased`, the attempt is current, and `lease_until > monotonic now`. A stale write changes nothing. [leases] |
| I3 | Each owner and agent write except `heartbeat` has a `request_id`; its reply is stored in the same transaction, keyed by a fingerprint of route, path, and body. Replay rules: `wire.py` header (a reply that ended the attempt replays always; any other only while the attempt is current). [store, each domain write] |
| I4 | A model reply and its tool calls commit in one `model` step before any tool starts. Each tool result commits before the next tool call or model call. The `tools` step commits only when every tool call of the pending step is `done`. [steps, calls, worker] |
| I5 | A tool call gets `run` at most once per attempt. A one-phase server tool runs inside `start_call`; a two-phase one commits only after a second fence. Both: exactly once. [calls] |
| I6 | When an attempt is lost or the server restarts, in the same transaction: each `running` `unsafe` tool call becomes `unknown`; `idempotent` ones get `run` again in the next attempt. [leases] |
| I7 | Only the owner changes an `unknown` tool call, and only while its agent is not terminal. An agent with one is `blocked`. [calls] |
| I8 | Each attempt gets a new sandbox copy at a path that no other attempt gets, cloned from a source whose writers have stopped (section 7). The claim reply comes only after the copy is ready. [sandboxes, leases] |
| I9 | Waiting is in the database: a pending child or command makes the worker yield; the transaction that fills the last pending tool call settles the agent. [steps, calls, tasks] |
| I10 | An activated attempt that is lost with no progress, or with a local tool call still `running`, adds a strike; else strikes reset. Backoff `min(1 s * 2^(strikes-1), 60 s)`. 3 strikes: `failed` (`poison`), unless a tool call is `unknown` (then `blocked`). [leases] |
| I11 | A model call starts only after its admission commits within the task budget. A call with no usage counts at its estimate; a reply whose usage passes the budget commits, but none of its tools runs. [steps] |
| I12 | A payload runs only after its grant commits. A launch is granted at most once, never after its deadline or a revoke. Only the current launch changes a process. A granted process is never launched again. [processes, shim] |
| I13 | An exit code comes only from the shim (report or `exit.json`). With neither, the outcome is `unknown`. [processes, shim] |
| I14 | The server, the client library, and the supervisor do not import `ai`. Messages are opaque JSON. [packages] |
| I15 | A worker that cannot renew its lease before its local deadline stops itself (`os._exit`). [worker.lease] |
| I16 | A cut command (outcome `unknown`, or a stop by the owner or a cancel) by policy class: `local` gives the model outcome `interrupted`; `external` makes the tool call `unknown` (I7). A timeout or an agent stop is a normal result. [processes] |
| I17 | `done` and `failed` are final. An agent's end fills its parent's tool call, stops its `agent` processes, fails its open descendants when it failed, and (root) ends the task. [tasks] |

Proof sketch (as run 1): by I1 each acknowledged fact is in SQLite; clients read only the server; a worker holds a
cache, code, and unacknowledged work; that work is a model call (safe to repeat) or a `running` tool call (I6); by
I2, I3, I8 a late or retried worker cannot change a committed fact or a file that a later attempt reads.

## 3. Components and endpoint

```
 owner: python -m swarm.owner / curl          (HTTP, owner token)
            |
 +----------v----------------------------------------------------+
 | server  (python -m swarm.server, own session, no ai)          |
 |   http.py   routes from wire.ROUTES, auth, Deps, JSON <-> wire|
 |   domain    tasks leases steps calls sandboxes processes      |
 |   store.py  SQLite WAL (schema.sql)     reaper thread 0.5 s   |
 |   files     sandboxes/<agent>/a<N>/   processes/<id>/         |
 +------^-------------------------^-----------------------^------+
        | agent API (worker token)| runner API            | launch API (single-use secret)
 +------+-------------+    +------+----------------------+ |
 | worker (own session)|<--| supervisor (own session)    | |
 |  lease loop external|   |  pool: start, restart       | |
 |  tools models       |   |  runner: order -> shim -----+-+--> shim (own session) -> payload (own group)
 +---------------------+   +-----------------------------+
   all clients use swarm.client; all share swarm.wire
```

Endpoint: the server listens on a stable URL. During an outage the clients wait it out: agent writes retry to the
local deadline; the runner backs off (0.5 s to 10 s) and starts a fresh `next_launch` poll (a lost order is never
reused), and it keeps reaping shims; shims keep their payload and retry `report_exit` (`exit.json` stays on disk).
 `--port N` is saved in `state_dir/server.json`; with no `--port`, the
saved port is used again, else 8700 (`--port 0` picks a free port once and saves it). Thus a restarted server has
the same URL, and the workers, the runner, and the shims reach it again with the credentials that they hold.

## 4. Layout

```
proto-clean/   ARCHITECTURE.md  README.md (part D)  pyproject.toml  uv.lock  .gitignore
  swarm/
    wire.py               REAL. Typed requests, replies, views, ROUTES, replay rules, server tool args
    server/
      __main__.py         python -m swarm.server --state DIR [--port N] [--policy FILE]
      app.py              Server: lock, tokens, startup order, reaper, health
      config.py           ServerConfig (TTL, clone TTL, strikes, backoff, launch limits, paths)
      errors.py           ApiError: the refusal kind; http.py maps it to a status
      http.py             Deps (the injection seam), route matching, auth, validation, error replies
      store.py            Store, Tx (dict rows, wall and monotonic now), stored replies, events, wake-ups
      schema.sql          REAL. 11 tables
      tasks.py            tasks, tree, cancel, history, events, run_child, memory_set, agent ends
      leases.py           claim (reserve, clone, publish), heartbeat, fence, attempt ends, strikes, settle
      steps.py            admission and budget, commit_step by kind, yield, finish
      calls.py            start_call (run grant, one- and two-phase server tools), finish, resolve, fill
      sandboxes.py        reserve, materialize, clone_tree (regular files only)
      processes.py        process records, launches, grant, shim heartbeat, exit, logs, process tools
      policy.py           CommandPolicy, AllowList, default_policy, load_policy
    client/api.py         HttpClient, OwnerClient, AgentClient, RunnerClient, LaunchClient
    worker/
      __main__.py         python -m swarm.worker --server URL [--kinds ..] [--tools M:A]; main(model_factory=)
      lease.py            Lease: first heartbeat, renewal thread, local deadline, pause/resume rule
      loop.py             kind loop: the step loop on ai.stream and ToolRunner
      tools.py            Registry, ToolEntry, CallContext, no-follow file tools, server tool stubs
      models.py           ModelFactory, gateway_model, estimate_tokens
      external.py         kind external: one Fx turn as one unsafe tool call
    owner/__main__.py     python -m swarm.owner --state DIR submit|show|history|events|resolve|cancel|procs|log|stop
    supervisor/
      __main__.py         python -m swarm.supervisor --state DIR --workers N [--worker-command ...]
      pool.py             WorkerPool: N slots, own sessions, restart backoff
      runner.py           Runner: next_launch long poll, start shim, order on stdin
      shim.py             barrier, grant, release, log with cap, heartbeat, stop, exit.json, exit report
  tests/  server/  worker/  supervisor/  e2e/  fakes.py  helpers.py   (by part, section 11)
```

## 5. API

Models and the replay rules are in `swarm/wire.py`; the table is `wire.ROUTES`. Errors: `wire.ErrorReply` with `401`,
`404`, `409 stale|conflict`, `422 invalid`, `503 busy`, `500`. Path parameters `*_seq` are integers.

| Route | Method, path | Auth | Request -> reply |
|---|---|---|---|
| health | GET /healthz | none | - -> Health |
| create_task | POST /v1/tasks | owner | CreateTask -> TaskCreated |
| get_task | GET /v1/tasks/{task_id} | owner | - -> TaskView |
| cancel_task | POST /v1/tasks/{task_id}/cancel | owner | OwnerWrite -> TaskView |
| get_history | GET /v1/agents/{agent_id}/history | owner | ?after_seq&limit -> HistoryPage |
| list_events | GET /v1/events | owner | ?task_id&after&limit&wait_s -> EventsPage |
| resolve_call | POST /v1/agents/{agent_id}/calls/{step_seq}/{call_id}/resolve | owner | ResolveCall -> CallView |
| list_processes | GET /v1/tasks/{task_id}/processes | owner | - -> ProcessList |
| read_log | GET /v1/processes/{process_id}/log | owner | ?offset&max_bytes -> LogChunk |
| stop_process | POST /v1/processes/{process_id}/stop | owner | OwnerWrite -> ProcessView |
| claim | POST /v1/claims | worker | ClaimRequest -> Claim, or 204 |
| heartbeat | POST /v1/agents/{agent_id}/heartbeat | worker | Heartbeat -> HeartbeatReply |
| admit_model_call | POST /v1/agents/{agent_id}/model-calls | worker | ModelCallRequest -> ModelCallReply |
| commit_step | POST /v1/agents/{agent_id}/steps | worker | StepRequest (kind user, model, tools) -> StepReply |
| start_call | POST /v1/agents/{agent_id}/calls/{step_seq}/{call_id}/start | worker | AgentWrite -> CallStartReply |
| finish_call | POST .../calls/{step_seq}/{call_id}/finish | worker | CallFinish -> CallView |
| yield_agent | POST /v1/agents/{agent_id}/yield | worker | AgentWrite -> YieldReply |
| finish_agent | POST /v1/agents/{agent_id}/finish | worker | FinishRequest -> AgentStateReply |
| next_launch | POST /v1/runner/launches | runner | NextLaunchRequest -> LaunchOrder, or 204 |
| grant_launch | POST /v1/launches/{launch_id}/grant | launch | GrantRequest -> GrantReply |
| launch_heartbeat | POST /v1/launches/{launch_id}/heartbeat | launch | LaunchHeartbeat -> LaunchHeartbeatReply |
| report_exit | POST /v1/launches/{launch_id}/exit | launch | ExitReport -> Ack |

Server tools (args in `wire.SERVER_TOOL_ARGS`): `memory_set`, `run_child(prompt, tools?)` (wait), `run_command(argv,
timeout_s)` (wait, barrier; result `wire.CommandResult`; its 4 KiB output tail comes from the shim's `ExitReport`,
empty when no receipt exists), `process_start(argv, lifetime)`
and `process_read(process_id, offset, max_bytes)` (two-phase), `process_stop(process_id)`. Local tools: `read_file`,
`write_file`, `list_files`, and `--tools` entries. Kind `external` only: `cli_turn`.

## 6. Schema

`swarm/server/schema.sql` is the source: `tasks` -> `agents` (tree by `parent_id`, pinned child seed, state, attempt,
monotonic lease, strikes) -> `attempts` (worker, pid, claim request, activated, progress, end reason, sandbox copy);
`messages` (step kind, opaque JSON); `calls` (tool calls: ordinal, safety, state, result, child, process); `memory`; `model_calls`
(estimate, usage); `requests` (fingerprint, reply, terminal flag); `events`; `processes` (approved argv and env, cwd,
class, lifetime, desired, stop reason, current launch, log facts) -> `launches` (secret hash, deadline, pid, grant
and exit hashes, exit receipt). Columns `*_mono` hold the monotonic clock of the current server run.

## 7. Flows and state tables

Run a task. Owner `create_task` -> root `ready`. Claim (below) -> attempt 1, copy `a1` from `spec.workspace`. Commit
`user` step; admit; `ai.stream`; commit the `model` step with its tool calls; for each: `start_call` -> `run`
(local tool, then `finish_call`) or `use`; commit the `tools` step; repeat. A `model` step with no tool calls:
`finish_agent(done)`; the task is `done`.

Claim (I3, I8). (1) One transaction: pick the agent, attempt+1, `leased` with the clone TTL, attempt row with the
claim request ID, reserve the copy path, stored reply NULL. (2) Clone with no transaction open. (3) One transaction:
fence that attempt, copy `ready`, normal TTL, store the reply. A retry with the same ID waits for step 3 and gets
the same claim. Replay of a claim ID checks its reserved attempt first: ended -> `409 stale` (the worker makes a new
ID), still cloning at the poll end -> `503 busy`, ready and current -> the stored claim. A clone error: attempt
`setup_failed`, agent `ready` again (3 tries, then `failed` `sandbox`). The source is the agent's latest ready copy;
with none yet (whatever the attempt number), the root workspace or the pinned child seed.
Source stability: a source copy belongs to an ended attempt (a dead or yielded worker); `run_command` ended before
the agent became ready; `process_start` payloads run in their own clone. A child's first claim waits until its
pinned seed attempt (the parent attempt that called `run_child`) has ended, then clones that copy.

Crash and resume. `kill -9` of the worker while a tool call is `running`. The reaper sees `lease_until` pass:
attempt `lost`; `unsafe` running -> `unknown` (I6); strike unless the attempt never sent a heartbeat (I10); settle.
The pool starts a new worker; it claims attempt 2 with copy `a2` from `a1`; `done` tool calls give `use`; the
`idempotent` one gets `run` again. An attempt-1 write that comes late: `409 stale` (I2). Server crash: workers retry
to their local deadline and exit (I15); the restarted server (same URL) ends all attempts `server_restart`, no strike.

Agent states. `settle` (no lease) by precedence; the first row that matches wins:

| Condition | State |
|---|---|
| agent `done` or `failed` | unchanged (final) |
| a tool call of the pending step is `unknown` | `blocked` (also wins over poison) |
| one is `waiting` | `waiting` |
| else (calls after a barrier can still be `planned`) | `ready` (after `ready_at`) |

A fill (child end, process end) while the agent is leased changes only the tool call; the worker's `yield` sees all
`done` and gets `continue`. Owner `resolve_call` on a terminal agent: `409 conflict`. A failed agent fails its open
descendants (`ancestor_failed`); cancel fails every open agent of the task (`cancelled`).

Child agents. `run_child` twice in one step: each `start_call` creates a child (`ready`, seed pinned) and sets the
tool call `waiting` (I5). Not a barrier, so both start. The parent yields (attempt `yielded`, `waiting`). Each child
runs; its end fills the parent's tool call with `{agent_id, status, result}` (I17). The last fill settles the
parent to `ready`; attempt N+1 commits the `tools` step. A poisoned child gives an error result, not a hang.

A background process. `process_start` (two-phase: policy; clone the copy to a new provisional
`processes/<id>/work` per try; then the stored-reply check, fence, still `planned`, commit; a clone error commits an
error result) -> process `requested` with the approved argv and env. Runner `next_launch` -> launch `issued` and an
order with the secret; the runner starts a shim. The shim holds the payload, gets the grant, releases it, writes the
log, heartbeats. All workers die: the shim and payload live; a new attempt reads the log from its offset
(`process_read`, two-phase). The exit report ends the process; a `command` process fills its `run_command` call.

Launch transitions. Only `processes.current_launch` changes the process row.

| Launch | Event | Result |
|---|---|---|
| issued | grant, before deadline, desired `running` | `granted` (pid, grant hash); process `running`, `granted_at` |
| issued | grant after deadline, or desired `stopped` | `granted=False`; shim kills the held child, reports `started=False` |
| issued | deadline passes (reaper) or server startup | `revoked`; process `requested` again, or `failed` `not_started` at 3 tries |
| issued, current | report with `started=False` (denied) | as a revoke, one transaction: launch `ended`; process `ended` `not_started` if desired `stopped`, else `requested` again or `failed` at 3 tries; waiters filled on a terminal result |
| not current | report with `started=False` (late) | launch `ended`; process unchanged |
| granted | same grant body | `granted` again (replay); another body: `granted=False` |
| granted | heartbeat | clock stored; `stop` if desired `stopped` or the command timeout passed (reason `timeout`) |
| revoked, ended, not current | heartbeat | `stop` |
| granted | exit report | `ended`; process `ended` (`exited`, `signaled`); `on_process_end` |
| ended | same report body / other body | `Ack` / `409 conflict` |
| granted | exit report (`exit.json` has the same `ExitReport`, with `output_tail`) | the tail fills `CommandResult` |
| granted | no heartbeat for 10 s | `exit.json` present: applied as a report; else process `ended` `unknown`, final size = file size |

## 8. Mechanisms of `proto/`: kept, simpler, deferred, removed

"Deferred" is what a production build adds; "removed" is test and demo code that no product needs.

| Mechanism | proto file | Verdict | Reason |
|---|---|---|---|
| Lease, attempt fence, reaper expiry | server/db.py, reaper.py | kept (I2) | Core of crash safety. |
| Request IDs with stored replies | server/db.py | kept (I3) | A lost reply must not give a second step or claim; replay rules now explicit. |
| Save each step before the next action | agent/worker.py, server/app.py | kept (I4) | The unit of durability. |
| Run grant once per attempt; server tools in a transaction | server/app.py | kept (I5) | Exactly once for server effects. |
| Unsafe -> unknown -> owner | server/reaper.py, app.py | kept (I6, I7) | No proof that a cut unsafe tool did nothing. |
| Workspace copy per attempt | server/tools.py | simpler (I8) | Automatic at claim; no `sandbox_open`, no CLI-home copy. |
| Wait for children in the database | server/tools.py, db.py | kept (I9) | Owner requirement. |
| Strikes, backoff, poison | server/reaper.py, db.py | kept (I10) | Bounds an agent that kills its worker each time. |
| Probation (struck jobs one at a time) | server/app.py | deferred | Finds the culprit of a repeated tree kill; poison bounds the cost. |
| Worker local deadline | agent/worker.py | kept (I15) | The worker stops before the server's lease ends. |
| Monotonic lease clock | server/db.py | kept (I2) | Startup ends all leases, so no server-run ID is needed. |
| One server per state directory | server/app.py | kept | Two servers would end each other's leases. |
| Long-poll disconnect check | server/app.py | simpler | Replaced by "a never-activated attempt gets no strike" and long transport timeouts. |
| Code version pin on claim | server/tools.py | deferred | One code tree per deployment here. |
| Model-call admission and budget | server/live.py | simpler (I11) | Per task, children included; no provider error taxonomy. |
| Delegation limits | server/live.py | kept | Spec fields `max_children`, `max_depth`. |
| Secret scans of tool results | agent/live.py | deferred | The key and token leave the worker env at start. |
| Launch barrier before `exec` | proc_shim.py | simpler (I12) | One barrier pipe; no exec-status pipe, no phase evidence files. |
| Single-use launch secret on stdin | proc_runner.py | kept | A shim acts only on its own launch. |
| Runner epoch | server/proc_launch.py | removed | One-transaction issue with a deadline gives the same safety here. |
| Reconciliation table, receipt import | proc_runner.py | simpler (I13) | Shim silence -> `exit.json` or `unknown`. |
| Process identity checks, signals table, leftover-group flags | proc_identity.py, proc_launch.py | deferred | Only the shim signals its own unreaped child (Limits L3). |
| Log cap at the writer; exact-byte reads; gaps | proc_shim.py, server/proc_log.py | kept | One log per process: only one launch is ever granted. |
| Cut-command rule by class | server/proc_effects.py | kept (I16) | The class comes from the policy. |
| Fixed recipes | proc_recipe.py | replaced | Real argv behind `CommandPolicy`. |
| Fx: cut policy, native store, launcher, MCP bridge, resume | agent/external.py, fx_*.py, server/external.py | simpler | One unsafe `cli_turn` call that blocks when cut; resume deferred (L6). |
| Memory gate and footprint monitor | DESIGN.md | deferred | Never built in run 1. |
| Orphan watch, setsid launcher, signal forwarding | supervisor.py | simpler | Workers start in their own sessions. |
| Simulated OOM, kill-point hooks, fixture scenarios, fixture endpoints, demos, launchd, commit ledgers | agent/tools.py, scenarios.py, demo-*.sh, launchd/ | removed | Test and demo code. Tests use the model factory and `--tools`. |

## 9. Decisions

- In the wild: a real `TaskSpec`; real argv behind `CommandPolicy` (every program `external` unless the owner's
  policy file says `local`); the model factory is given at construction (`swarm.worker.main(model_factory=...)`),
  so the product has no switch for tests; custom tools by `--tools`.
- Names: job is merged into agent (one run per agent); gen -> attempt; effect -> tool call; version -> sandbox copy;
  proc -> process; shell_run -> run_command; native -> loop. Process lifetime `command` (not "call").
- Stack: Python 3.13, uv, `ai==0.10.0`, `pydantic==2.14.3` (declared, as `wire.py` uses it). HTTP on the standard
  library (`ThreadingHTTPServer`, `urllib`): no new package; the lock has the same 15 packages as `proto/`.
- Scope cuts in revision 2, to pay for the fixes: the `checkpoint_call` route and column, Fx resume, the agent
  tools `memory_get` (memory is in the system message) and `process_list` (the owner route stays), the `--model-factory` switch, per-launch log cursors.
- Size: parts S + C + P are about 3,300 lines of logic plus `wire.py` (527 lines, mostly declarations): about 10%
  above the 3,000 aim, because the fixes added the claim reservation, two-phase tools, and the launch table. Each
  file stays below 300 lines except `wire.py`, which is one file so that the contract is in one place.

## 10. Code style (all parts)

- Names: the terms of section 1, nothing else; no run-1 names or codes (job, gen, effect, W10, P3). Full words.
- Every public function has type hints and a one-line docstring; the stub docstrings are the spec; keep them.
- A domain write reads: (1) stored-reply check, (2) fence or auth, (3) validate, (4) change rows and write the event,
  (5) store and return the reply. One transaction unless it is a two-phase operation.
- Functions under about 40 lines; files under 300. No class where a function will do.
- A comment says why, not what, and names the invariant: `# I6: a cut unsafe call can have had its effect`.
- Errors: domain code raises `ApiError(kind)`; only `http.py` knows status codes.
- No test hook, environment switch, or fixture in product code. Tests use the model factory, `--tools`,
  `--worker-command`, real signals, and the public APIs.
- Standard library first; `ai` only in `swarm/worker/`.

## 11. Tests

Public interfaces only; real subprocesses on a temporary state directory; `unittest`. Fake models: one full
`FakeModel` script per test that matches the history that a new worker rebuilds (fixed call IDs, built with
`ToolCallPart`, not `ai.testing.tool_call`); a new factory instance gives the same script. Test coordination
(for example "kill now") reads the server through `OwnerClient`; it never adds code to the product.

| # | Test | Part |
|---|---|---|
| T1 | e2e with `FakeModel`: write_file, memory_set, final text -> `done`; history, memory, file in the last copy | D |
| T2 | `kill -9` of the worker (attempt pid) during a slow idempotent test tool; the pool restarts it; `done`; `runs == 2`; no duplicate step | D |
| T3 | Kill the supervisor and every worker; the owner API shows all rows; a new supervisor finishes the task | D |
| T4 | Stale attempt: an attempt-1 write after attempt 2 claimed -> 409, no change | S |
| T5 | Replay: same ID and body -> same reply; other body -> 422; a lost claim reply retried -> the same claim; a released `yield` replayed after the attempt ended | S |
| T6 | Lease lost during an unsafe tool call -> `blocked`; `resolve_call` -> the next attempt finishes; resolve after cancel -> 409 | S, D |
| T7 | Three activated losses with no progress -> `poison`; parent gets an error result; a never-activated loss adds no strike | S |
| T8 | Parent with two `run_child`: both run, parent yields and wakes, finishes; children seeded from the pinned parent copy | D |
| T9 | Copy per attempt: new path, old files present; a live old writer (a test process writing to `a1`) is not seen by `a2` | D |
| T10 | `process_start`; kill all workers; the process lives; a new attempt reads from its offset; `process_stop` -> `signaled` | D |
| T11 | `run_command`: agent `waiting`, then `CommandResult` with exit code and output tail | D |
| T12 | Policy: a refused program gives an error result and no process; `local` from a policy file | S |
| T13 | Kill the shim during `run_command`: `unknown`; `local` -> `interrupted`; `external` -> `blocked`; with `exit.json` the real code | D |
| T14 | Budget: refused admission -> `failed` `budget`; usage over budget -> no tool of that reply runs | S |
| T15 | Restart the server during a lease: same URL; attempt `server_restart`, strikes 0; the shim of a running process reports to the new server; `done` | D |
| T16 | Launch table: one grant; grant replay; no grant after the deadline; startup revokes `issued`; a heartbeat for a non-current launch gets `stop` | S, P |
| T17 | Import of the server, client, and supervisor packages in a fresh interpreter: `ai` not in `sys.modules` | S |
| T18 | Cancel: agents `failed` `cancelled`, processes stopped, the old worker's next write -> 409 | S |
| T19 | Optional, skipped with no key or no `fx`: live Gateway run with a kill; one live Fx turn | D |

## 12. Limits

What can still go wrong in this prototype, and what a production build adds.

| # | Limit | Production adds |
|---|---|---|
| L1 | A stopped (SIGSTOP) or escaped old writer can change a source copy while it is cloned. | Clone from a frozen snapshot (APFS snapshot) or a VM boundary. |
| L2 | A same-user process can swap a path component under a file tool between checks. | A separate user or sandbox profile per agent. |
| L3 | A payload that outlives its shim (or leaves its group) is not signaled; its outcome is `unknown`. | Identity-checked reconciliation (birth time, boot ID) and group cleanup, as in run 1. |
| L4 | A command runs as the owner's user with no file or network confinement; `local` is an owner assertion. | A sandbox profile or separate user; reviewed command classes. |
| L5 | A stopped shim (SIGSTOP) longer than 10 s gets `unknown` though its payload may still run. | Identity checks before the verdict (L3). |
| L6 | An Fx turn cut by a worker loss blocks for the owner; the Fx process can outlive the worker and use its own login and real `HOME`. | Attempt-scoped Fx home, native resume, process-group cleanup, an MCP bridge for server tools. |
| L7 | No memory limit for a process tree on macOS; a real OOM can kill the server. | A footprint monitor that pauses claims and kills the largest verified group. |

## 13. Build parts

Three builders in parallel on disjoint paths, then one integrates. Frozen: `swarm/wire.py`, `schema.sql`, the stub
signatures; a needed change goes to the Board thread and part D applies it. Commit with `SWARM=1 git commit` on
your paths only. Run in `proto-clean/` after `uv sync --locked --python 3.13 --exclude-newer-package ai=2026-11-19`.

| Part | Owns | Must meet | Size (lines) | Proof command |
|---|---|---|---|---|
| S: server | `swarm/server/**` (not schema.sql), `tests/server/**` | Every route; I1-I14, I16, I17; sections 3 and 7; startup order in `app.py`; `server.json` = {url, port, pid}; `tokens.json` keys `owner`, `worker`, `runner` | about 1,780: store 150, errors 15, http 170, app 110, config+main 70, policy 50, tasks 210, leases 240, steps 170, calls 200, sandboxes 90, processes 300 | `uv run --no-sync python -m unittest discover -s tests/server -t . -v` (T4, T5, T6, T7, T12, T14, T16, T17, T18; `urllib` and `swarm.wire` only) |
| C: clients | `swarm/client/**`, `swarm/worker/**`, `swarm/owner/**`, `tests/worker/**` | Signatures in `client/api.py`; the loop and lease rules; I4, I15 on the worker side; env scrub at start | about 1,060: client 220, lease 110, loop 240, tools 170, models 70, worker main 60, owner 80, external 110 | `uv run --no-sync python -m unittest discover -s tests/worker -t . -v`: the loop with `FakeModel` against an in-memory `AgentClient` stand-in; resume from a history cut at each step; client retries keep the request ID (stdlib stub server) |
| P: processes | `swarm/supervisor/**`, `tests/supervisor/**` | Shim steps in `shim.py`; secret on stdin only; payload env from the order only; pool backoff; no `ai` | about 480: pool 120, runner 90, shim 220, main 50 | `uv run --no-sync python -m unittest discover -s tests/supervisor -t . -v`: shim with a stand-in `LaunchClient`: denied -> no payload run; granted -> exit code, log bytes, `exit.json`; stop -> TERM then KILL; log cap; heartbeat failures do not stop draining; pool restarts a dying command |
| D: integration | `tests/e2e/**`, `tests/fakes.py`, `tests/helpers.py`, `README.md`; seam fixes anywhere, each listed | T1-T19; README with run commands | about 700 tests, 80 README | `uv run --no-sync python -m unittest discover -s tests -t . -v` exit 0, then `pgrep -fl 'swarm\.(server|worker|supervisor)'` prints nothing |

Order: S, C, P start at once; C writes `client/api.py` first. D starts after all three report. Each test cleans up
every process that it starts.

## 14. Not verified by the planner

- Only `wire.py` and the schema have bodies; sizes are estimates.
- APFS `clonefile` through ctypes and `O_NOFOLLOW` descriptor-relative file tools: from run 1, not tried here.
- `FakeModel` matching across a worker restart with full-history scripts: read in `ai/testing.py`, not run.
