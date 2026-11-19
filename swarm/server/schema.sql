-- The server's state. One SQLite file, WAL mode. The server is the only writer.
-- Two clocks (I2, I12). Columns named *_mono (lease_until, grant_deadline, heartbeat_mono)
-- hold time.monotonic() of the current server run: a wall-clock jump cannot move a lease.
-- They have no meaning after a restart, so startup ends every lease, revokes every issued
-- launch, and sets heartbeat_mono of granted launches to its own now. All other times
-- (*_at) are wall-clock seconds, for people and for command timeouts only.

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    root_agent_id TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('running', 'done', 'failed', 'cancelled')),
    budget_tokens INTEGER NOT NULL,
    created_at    REAL NOT NULL
);

-- An agent is a record, not a process. It has one run: its state, lease, and
-- attempt counter are on this row. A worker process holds it only through a lease.
CREATE TABLE IF NOT EXISTS agents (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    parent_id       TEXT REFERENCES agents(id),
    parent_call     TEXT,                        -- "<step_seq>:<call_id>" of the parent's run_child call
    seed_agent_id   TEXT REFERENCES agents(id),  -- child: the parent; its sandbox copy seeds the child
    seed_attempt    INTEGER,                     -- child: the parent attempt that made the call (pinned)
    depth           INTEGER NOT NULL DEFAULT 0,
    kind            TEXT NOT NULL CHECK (kind IN ('loop', 'external')),
    spec_json       TEXT NOT NULL,               -- wire.TaskSpec; for a child, the derived spec
    state           TEXT NOT NULL CHECK (state IN ('ready', 'leased', 'waiting', 'blocked', 'done', 'failed')),
    attempt         INTEGER NOT NULL DEFAULT 0,  -- number of the current or last attempt (the fence)
    lease_until     REAL,                        -- monotonic
    clone_tries     INTEGER NOT NULL DEFAULT 0,
    strikes         INTEGER NOT NULL DEFAULT 0,
    ready_at        REAL NOT NULL DEFAULT 0,     -- backoff after a strike
    result_json     TEXT,
    fail_reason     TEXT,                        -- poison | budget | cancelled | ancestor_failed | sandbox | worker text
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS agents_task ON agents(task_id);
CREATE INDEX IF NOT EXISTS agents_ready ON agents(state, ready_at);

-- One try of an agent by one worker process. The sandbox copy belongs to the attempt.
CREATE TABLE IF NOT EXISTS attempts (
    agent_id      TEXT NOT NULL REFERENCES agents(id),
    attempt       INTEGER NOT NULL,
    worker_id     TEXT NOT NULL,
    pid           INTEGER NOT NULL,
    claim_request TEXT NOT NULL,                 -- request_id of the claim that made it (reservation)
    activated     INTEGER NOT NULL DEFAULT 0,    -- 1 after the first heartbeat; a never-activated loss is no strike
    started_at    REAL NOT NULL,
    ended_at      REAL,
    end_reason    TEXT CHECK (end_reason IN ('yielded', 'finished', 'lost', 'server_restart', 'cancelled', 'setup_failed')),
    progress      INTEGER NOT NULL DEFAULT 0,    -- 1 after the first committed step or call result
    sandbox_path  TEXT NOT NULL,
    sandbox_state TEXT NOT NULL CHECK (sandbox_state IN ('cloning', 'ready')),
    PRIMARY KEY (agent_id, attempt)
);

-- Message history. seq starts at 0. `json` is SDK Message JSON; the server does not parse it.
CREATE TABLE IF NOT EXISTS messages (
    agent_id TEXT NOT NULL REFERENCES agents(id),
    seq      INTEGER NOT NULL,
    attempt  INTEGER NOT NULL,
    kind     TEXT NOT NULL CHECK (kind IN ('user', 'model', 'tools')),  -- wire.StepRequest.kind
    role     TEXT NOT NULL,
    json     TEXT NOT NULL,
    PRIMARY KEY (agent_id, seq)
);

-- One tool call of a committed model reply, with its durable state and result.
CREATE TABLE IF NOT EXISTS calls (
    agent_id        TEXT NOT NULL REFERENCES agents(id),
    step_seq        INTEGER NOT NULL,            -- seq of the model reply that planned it
    call_id         TEXT NOT NULL,               -- tool_call_id from the model
    ordinal         INTEGER NOT NULL,            -- position in StepRequest.calls; orders the tool message
    tool            TEXT NOT NULL,
    args_json       TEXT NOT NULL,
    safety          TEXT NOT NULL CHECK (safety IN ('server', 'idempotent', 'unsafe')),
    state           TEXT NOT NULL CHECK (state IN ('planned', 'running', 'waiting', 'done', 'unknown')),
    attempt         INTEGER,                     -- the attempt that got `run`
    runs            INTEGER NOT NULL DEFAULT 0,
    result_json     TEXT,                        -- wire.CallResult (an `error` call: done at commit)
    child_agent_id  TEXT REFERENCES agents(id),
    process_id      TEXT REFERENCES processes(id),
    PRIMARY KEY (agent_id, step_seq, call_id)
);

CREATE TABLE IF NOT EXISTS memory (
    agent_id   TEXT NOT NULL REFERENCES agents(id),
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    version    INTEGER NOT NULL,
    PRIMARY KEY (agent_id, key)
);

-- Each model call is admitted before it starts; an admitted call with no usage
-- counts at its estimate. The task budget is the sum over all agents of the task.
CREATE TABLE IF NOT EXISTS model_calls (
    agent_id         TEXT NOT NULL REFERENCES agents(id),
    attempt          INTEGER NOT NULL,
    step_seq         INTEGER NOT NULL,
    task_id          TEXT NOT NULL REFERENCES tasks(id),
    estimated_tokens INTEGER NOT NULL,
    used_tokens      INTEGER,                    -- input + output, set by the step that commits the reply
    admitted_at      REAL NOT NULL,
    PRIMARY KEY (agent_id, attempt, step_seq)
);

-- Idempotency (I3): the reply of each mutation, stored in the same transaction.
-- scope: 'owner', 'worker:<worker_id>' for claims, '<agent_id>:<attempt>' for agent writes.
-- fingerprint: sha256 of route name, path parameters, body. reply_json NULL: a claim whose
-- sandbox copy is being made (its retry waits). terminal=1: the reply ended the attempt,
-- so it is replayed even after the attempt is over.
CREATE TABLE IF NOT EXISTS requests (
    scope        TEXT NOT NULL,
    request_id   TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    reply_json   TEXT,
    terminal     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, request_id)
);

CREATE TABLE IF NOT EXISTS events (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    task_id  TEXT NOT NULL,
    agent_id TEXT,
    type     TEXT NOT NULL,
    json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, seq);

-- A background command. The server owns the record; a shim outside the server
-- tree is the parent of the payload.
CREATE TABLE IF NOT EXISTS processes (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks(id),
    agent_id     TEXT NOT NULL REFERENCES agents(id),
    call_ref     TEXT NOT NULL UNIQUE,           -- "<agent_id>:<step_seq>:<call_id>" that made it
    argv_json    TEXT NOT NULL,                  -- the checked argv (argv[0] a bare name, found by PATH in env_json)
    env_json     TEXT NOT NULL,                  -- the full payload environment from the policy
    cwd          TEXT NOT NULL,                  -- run_command: the attempt's copy; process_start: own clone
    effect_class TEXT NOT NULL CHECK (effect_class IN ('local', 'external')),
    lifetime     TEXT NOT NULL CHECK (lifetime IN ('command', 'agent', 'task')),
    timeout_s    REAL,                           -- command only; counted from granted_at
    desired      TEXT NOT NULL CHECK (desired IN ('running', 'stopped')),
    stop_reason  TEXT CHECK (stop_reason IN ('agent', 'owner', 'timeout', 'agent_ended', 'task_ended', 'cancelled')),
    current_launch TEXT,                         -- the only launch whose reports change this row
    state        TEXT NOT NULL CHECK (state IN ('requested', 'launching', 'running', 'ended', 'failed')),
    outcome      TEXT CHECK (outcome IN ('not_started', 'exited', 'signaled', 'unknown')),
    exit_code    INTEGER,
    signal       INTEGER,
    tries        INTEGER NOT NULL DEFAULT 0,
    log_path     TEXT NOT NULL,                  -- one log per process: only one launch is ever granted
    log_bytes    INTEGER NOT NULL DEFAULT 0,     -- final when state is ended
    truncated_bytes INTEGER NOT NULL DEFAULT 0,
    granted_at   REAL,
    created_at   REAL NOT NULL,
    ended_at     REAL
);
CREATE INDEX IF NOT EXISTS processes_state ON processes(state);

-- One try to start a process. The payload runs only after `granted` commits.
CREATE TABLE IF NOT EXISTS launches (
    id             TEXT PRIMARY KEY,
    process_id     TEXT NOT NULL REFERENCES processes(id),
    secret_hash    TEXT NOT NULL,                -- sha256 of the single-use secret
    state          TEXT NOT NULL CHECK (state IN ('issued', 'granted', 'revoked', 'ended')),
    grant_deadline REAL NOT NULL,                -- monotonic
    pid            INTEGER,                      -- payload pid = pgid, stored with the grant
    grant_hash     TEXT,                         -- body hash of the grant, for replay
    heartbeat_mono REAL,                         -- monotonic time of the last shim heartbeat
    exit_hash      TEXT,                         -- body hash of the exit report, for replay
    exit_json      TEXT,                         -- wire.ExitReport (with output_tail), reported or from exit.json
    issued_at      REAL NOT NULL,
    ended_at       REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS launches_one_active ON launches(process_id)
    WHERE state IN ('issued', 'granted');
