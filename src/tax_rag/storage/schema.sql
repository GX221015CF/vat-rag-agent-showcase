PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    title TEXT NOT NULL UNIQUE,
    legal_level TEXT NOT NULL,
    approval_status TEXT NOT NULL CHECK (approval_status IN ('APPROVED_ACTIVE', 'PENDING_REVIEW', 'REJECTED', 'SUPERSEDED')),
    current_version_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_versions (
    version_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    source_filename TEXT NOT NULL,
    source_relative_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL UNIQUE,
    copy_sha256 TEXT NOT NULL,
    source_date TEXT,
    parser_rule_version TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    version_id TEXT NOT NULL REFERENCES document_versions(version_id),
    ordinal INTEGER NOT NULL,
    anchor TEXT NOT NULL,
    anchor_role TEXT NOT NULL,
    article_number INTEGER,
    path_json TEXT NOT NULL,
    text TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    source_locator_json TEXT NOT NULL,
    retrieval_enabled INTEGER NOT NULL CHECK (retrieval_enabled IN (0, 1)),
    UNIQUE(version_id, ordinal)
);

CREATE TABLE IF NOT EXISTS chunk_topics (
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
    topic_code TEXT NOT NULL,
    PRIMARY KEY(chunk_id, topic_code)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    document_id UNINDEXED,
    version_id UNINDEXED,
    search_text,
    tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    ingest_run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    source_count INTEGER NOT NULL,
    chunk_count INTEGER,
    detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_generations (
    generation_id TEXT PRIMARY KEY,
    collection_name TEXT NOT NULL UNIQUE,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER,
    chunk_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('CANDIDATE', 'ACTIVE', 'FAILED', 'SUPERSEDED')),
    created_at TEXT NOT NULL,
    activated_at TEXT
);

CREATE TABLE IF NOT EXISTS index_members (
    generation_id TEXT NOT NULL REFERENCES index_generations(generation_id),
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
    PRIMARY KEY(generation_id, chunk_id)
);

CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);

