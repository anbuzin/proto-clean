-- The server's state. One SQLite file, WAL mode. The server is the only writer.
-- Times are wall-clock seconds (time.time()). At startup the server ends every lease,
-- so a lease time from an old server run is never compared with a new clock.

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
    depth           INTEGER NOT NULL DEFAULT 0,
    kind            TEXT NOT NULL CHECK (kind IN ('loop', 'external')),
    spec_json       TEXT NOT NULL,               -- wire.TaskSpec; for a child, the derived spec
    state           TEXT NOT NULL CHECK (state IN ('ready', 'leased', 'waiting', 'blocked', 'done', 'failed')),
    attempt         INTEGER NOT NULL DEFAULT 0,  -- number of the current or last attempt (the fence)
    lease_until     REAL,
    strikes         INTEGER NOT NULL DEFAULT 0,
    ready_at        REAL NOT NULL DEFAULT 0,     -- backoff after a strike
    result_json     TEXT,
    fail_reason     TEXT,                        -- 'poison', 'budget', 'cancelled', or the worker's reason
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
    started_at    REAL NOT NULL,
    ended_at      REAL,
    end_reason    TEXT CHECK (end_reason IN ('yielded', 'finished', 'lost', 'server_restart', 'cancelled')),
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
    role     TEXT NOT NULL,
    json     TEXT NOT NULL,
    PRIMARY KEY (agent_id, seq)
);

-- One tool call of a committed model reply, with its durable state and result.
CREATE TABLE IF NOT EXISTS calls (
    agent_id        TEXT NOT NULL REFERENCES agents(id),
    step_seq        INTEGER NOT NULL,            -- seq of the model reply that planned it
    call_id         TEXT NOT NULL,               -- tool_call_id from the model
    tool            TEXT NOT NULL,
    args_json       TEXT NOT NULL,
    safety          TEXT NOT NULL CHECK (safety IN ('server', 'idempotent', 'unsafe')),
    state           TEXT NOT NULL CHECK (state IN ('planned', 'running', 'waiting', 'done', 'unknown')),
    attempt         INTEGER,                     -- the attempt that got `run`
    runs            INTEGER NOT NULL DEFAULT 0,
    checkpoint_json TEXT,                        -- resume data that a running local call saved
    result_json     TEXT,                        -- wire.CallResult
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

-- Idempotency: the reply of each mutation, stored in the same transaction.
-- scope: 'owner', 'worker:<worker_id>' for claims, '<agent_id>:<attempt>' for agent writes.
CREATE TABLE IF NOT EXISTS requests (
    scope        TEXT NOT NULL,
    request_id   TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    reply_json   TEXT NOT NULL,
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
    argv_json    TEXT NOT NULL,
    cwd          TEXT NOT NULL,                  -- the sandbox copy of the attempt that made it
    effect_class TEXT NOT NULL CHECK (effect_class IN ('local', 'external')),
    lifetime     TEXT NOT NULL CHECK (lifetime IN ('call', 'agent', 'task')),
    timeout_s    REAL,
    desired      TEXT NOT NULL CHECK (desired IN ('running', 'stopped')),
    stop_reason  TEXT,
    state        TEXT NOT NULL CHECK (state IN ('requested', 'launching', 'running', 'ended', 'failed')),
    outcome      TEXT CHECK (outcome IN ('not_started', 'exited', 'signaled', 'unknown')),
    exit_code    INTEGER,
    signal       INTEGER,
    tries        INTEGER NOT NULL DEFAULT 0,
    log_path     TEXT NOT NULL,
    log_bytes    INTEGER NOT NULL DEFAULT 0,
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
    grant_deadline REAL NOT NULL,
    pid            INTEGER,                      -- payload pid = pgid, stored with the grant
    heartbeat_at   REAL,
    issued_at      REAL NOT NULL,
    ended_at       REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS launches_one_active ON launches(process_id)
    WHERE state IN ('issued', 'granted');
