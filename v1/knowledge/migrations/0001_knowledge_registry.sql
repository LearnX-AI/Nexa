-- NEXA Knowledge Service registry, PostgreSQL (NEXA-ADR-0002, NEXA-M2-002).
-- Same tables and rules as the SQLite registry in v1/knowledge/ingestion/registry.py.
-- The adapter that uses this schema lands with the pgvector retrieval step; the
-- chunk table here is the system of record, vectors live alongside it.
BEGIN;

CREATE SCHEMA IF NOT EXISTS knowledge;

CREATE TABLE knowledge.sources (
    version_id      text PRIMARY KEY,                    -- source_id@<16 hex of checksum>
    source_id       text NOT NULL,
    checksum        text NOT NULL UNIQUE CHECK (checksum ~ '^sha256:[0-9a-f]{64}$'),
    metadata        jsonb NOT NULL,                      -- SourceMetadata 1.x
    status          text NOT NULL CHECK (status IN ('active', 'superseded', 'withdrawn')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_sources_source_id ON knowledge.sources (source_id);
CREATE INDEX ix_sources_meta ON knowledge.sources USING gin (metadata jsonb_path_ops);

CREATE TABLE knowledge.runs (
    run_id              text PRIMARY KEY,
    version_id          text NOT NULL REFERENCES knowledge.sources (version_id),
    pipeline_version    text NOT NULL,
    normalizer_version  text NOT NULL,
    chunker_version     text NOT NULL,
    chunk_config_hash   text NOT NULL,
    extractor           text NOT NULL,
    extractor_version   text NOT NULL,
    ocr                 jsonb,
    stats               jsonb NOT NULL,
    quality             jsonb NOT NULL,
    status              text NOT NULL CHECK (status IN ('succeeded', 'partial')),
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_runs_version ON knowledge.runs (version_id);

CREATE TABLE knowledge.jobs (
    job_id          text PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    checksum        text NOT NULL,
    file_name       text NOT NULL,
    mime_type       text,
    version_id      text,
    run_id          text,
    state           text NOT NULL CHECK (state IN ('queued', 'running', 'succeeded', 'partial', 'failed',
                                                   'quarantined', 'duplicate')),
    stage           text NOT NULL CHECK (stage IN ('validate', 'extract', 'scan', 'metadata', 'chunk',
                                                   'index', 'done')),
    attempts        integer NOT NULL DEFAULT 0,
    retryable       boolean NOT NULL DEFAULT false,
    error_code      text,
    error_detail    text,
    warnings        jsonb NOT NULL DEFAULT '[]',
    manifest        jsonb NOT NULL DEFAULT '{}',
    principal       text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_jobs_state ON knowledge.jobs (state);
CREATE INDEX ix_jobs_checksum ON knowledge.jobs (checksum);

CREATE TABLE knowledge.chunks (
    chunk_id        text PRIMARY KEY,
    version_id      text NOT NULL REFERENCES knowledge.sources (version_id),
    run_id          text NOT NULL REFERENCES knowledge.runs (run_id),
    ordinal         integer NOT NULL,
    active          boolean NOT NULL,
    embedded        boolean NOT NULL DEFAULT false,
    text            text NOT NULL,
    embedding_text  text NOT NULL,
    record          jsonb NOT NULL,                      -- full chunk record with provenance
    UNIQUE (run_id, ordinal)
);
CREATE INDEX ix_chunks_version_active ON knowledge.chunks (version_id) WHERE active;

COMMIT;
