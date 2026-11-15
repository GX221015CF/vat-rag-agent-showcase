PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS browser_consents (
    session_hash TEXT PRIMARY KEY,
    notice_version TEXT NOT NULL,
    accepted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matters (
    matter_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('DRAFT','ACTIVE','CLOSED','EXPIRED')),
    round_count INTEGER NOT NULL DEFAULT 0 CHECK(round_count BETWEEN 0 AND 8),
    notice_version TEXT NOT NULL,
    notice_accepted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    matter_id TEXT NOT NULL REFERENCES matters(matter_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('USER','ASSISTANT')),
    round_number INTEGER NOT NULL CHECK(round_number BETWEEN 1 AND 8),
    content TEXT NOT NULL,
    submission_token TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retrieval_runs (
    retrieval_run_id TEXT PRIMARY KEY,
    matter_id TEXT NOT NULL REFERENCES matters(matter_id) ON DELETE CASCADE,
    user_message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    route_status TEXT NOT NULL,
    route_reason TEXT NOT NULL,
    generation_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retrieval_hits (
    retrieval_run_id TEXT NOT NULL REFERENCES retrieval_runs(retrieval_run_id) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    chunk_id TEXT NOT NULL,
    document_title TEXT NOT NULL,
    anchor TEXT NOT NULL,
    text TEXT NOT NULL,
    source_json TEXT NOT NULL,
    rrf_score TEXT NOT NULL,
    channel_ranks_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(retrieval_run_id, rank)
);

CREATE TABLE IF NOT EXISTS calculation_runs (
    calculation_run_id TEXT PRIMARY KEY,
    matter_id TEXT NOT NULL REFERENCES matters(matter_id) ON DELETE CASCADE,
    calculation_type TEXT NOT NULL CHECK(calculation_type IN ('SINGLE_TRANSACTION','PERIOD_SUMMARY')),
    formula_version TEXT NOT NULL,
    rounding_rule_version TEXT NOT NULL,
    input_json TEXT NOT NULL,
    intermediate_json TEXT NOT NULL,
    output_json TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generation_runs (
    generation_run_id TEXT PRIMARY KEY,
    matter_id TEXT NOT NULL REFERENCES matters(matter_id) ON DELETE CASCADE,
    user_message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    retrieval_run_id TEXT NOT NULL REFERENCES retrieval_runs(retrieval_run_id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('SUCCESS','FAILED','VALIDATION_FAILED')),
    request_context_hash TEXT,
    response_id TEXT,
    response_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    matter_ref_hash TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_matter_created
ON messages(matter_id, created_at);

CREATE INDEX IF NOT EXISTS idx_retrieval_runs_matter_created
ON retrieval_runs(matter_id, created_at);

CREATE INDEX IF NOT EXISTS idx_calculation_runs_matter_created
ON calculation_runs(matter_id, created_at);

CREATE INDEX IF NOT EXISTS idx_generation_runs_matter_created
ON generation_runs(matter_id, created_at);

CREATE INDEX IF NOT EXISTS idx_audit_created
ON audit_events(created_at);
