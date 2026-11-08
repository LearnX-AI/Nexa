-- NEXA Knowledge Service vector index (NEXA-ADR-0002, NEXA-M2-004).
-- Run once per database as an administrator. The service creates the same
-- fixed tables itself if they are missing, but it cannot create the extension
-- or grant rights, so this file is the supported way to prepare a database.
--
--   psql -v svc_role=nexa_knowledge -f 0002_vector_index.sql "$ADMIN_DSN"
--
-- pgvector 0.5 or later is required (HNSW). Ubuntu 24.04: postgresql-16-pgvector.
BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS knowledge;

-- One row per index version; record holds the IndexRecord (model fingerprint,
-- status, build stats, validation report).
CREATE TABLE IF NOT EXISTS knowledge.index_versions (
    index_id   text PRIMARY KEY,
    status     text NOT NULL CHECK (status IN ('building', 'ready', 'active', 'retired', 'failed')),
    record     jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- The single 'active' pointer. Switching it is one transaction.
CREATE TABLE IF NOT EXISTS knowledge.index_alias (
    name     text PRIMARY KEY,
    index_id text NOT NULL REFERENCES knowledge.index_versions (index_id)
);

-- Content-addressed embedding cache: (model fingerprint, sha256 of the
-- prefixed text) -> vector. Rebuilding an index for an unchanged corpus and
-- model makes no gateway calls.
CREATE TABLE IF NOT EXISTS knowledge.embedding_cache (
    fingerprint text NOT NULL,
    text_key    text NOT NULL,
    embedding   real[] NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (fingerprint, text_key)
);

-- Per-index tables (knowledge.vec_<index_id>) are created by the service when a
-- build starts and dropped when the index is retired, so the service role owns
-- the schema's objects but needs no superuser rights.
GRANT USAGE, CREATE ON SCHEMA knowledge TO :"svc_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge.index_versions, knowledge.index_alias,
      knowledge.embedding_cache TO :"svc_role";

COMMIT;
