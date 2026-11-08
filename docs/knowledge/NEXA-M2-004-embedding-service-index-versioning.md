# NEXA-M2-004: Embedding Service and Index Versioning

Status: Draft for governance acceptance
Milestone: M2
Aligns with: NEXA-ADR-0001 (Model Gateway), NEXA-ADR-0002 (PostgreSQL + pgvector), NEXA-ADR-0004
Implementation: `v1/knowledge/embeddings.py`, `indexing.py`, `store.py`, `pgstore.py`,
`migrations/0002_vector_index.sql`; endpoints in `main.py`
Evidence: `docs/knowledge/evidence/m2-004-index-bench-offline.json`

## 1. What this adds

| Before (M1 to M2-003) | Now |
|---|---|
| In-memory vector list, lost on restart | pgvector store on PostgreSQL (ADR-0002), persistent; in-memory store kept for tests |
| Vectors carry no record of the model that made them | Every index version is stamped with a model **fingerprint**; vectors from two models are never compared |
| One implicit index | Versioned indexes: build beside the live one, validate, activate atomically, roll back, retire |
| Gateway model change goes unnoticed; scores silently become meaningless | Queries are refused (HTTP 503) when the gateway's model no longer matches the active index |
| nomic task prefixes dropped (regression from v0) | Prefixes restored, chosen from the model the gateway reports |
| Every re-index re-embeds everything | Content-addressed embedding cache: an unchanged corpus costs zero gateway calls |
| Re-indexing a source deletes, then re-adds | New chunks are written first, then stale ones removed: no gap in retrieval |

## 2. Embedding service

All embedding still goes through the Model Gateway (ADR-0001).

- **Model identity.** Each gateway reply carries `model_version` and `runtime`
  (from the gateway's model registry). With the vector dimension and the
  prefix scheme these make the fingerprint, e.g.
  `nexa-embed-default|nomic-embed-text|ollama|768|nomic-embed`.
- **Task prefixes.** Asymmetric retrieval models need different prefixes for
  documents and queries. v0 used them; its own comment notes that without them
  "retrieval returns the wrong subject" (main.py, `PrefixedOllamaEmbeddings`).
  The M1 client did not. Schemes are chosen from the reported model version:

  | Model family | Document prefix | Query prefix |
  |---|---|---|
  | nomic-embed | `search_document: ` | `search_query: ` |
  | e5, multilingual-e5 | `passage: ` | `query: ` |
  | bge, mxbai-embed | none | `Represent this sentence for searching relevant passages: ` |
  | anything else | none | none |

  `NEXA_EMBED_PREFIX_SCHEME` can force `none` or `custom`. The scheme is part
  of the fingerprint, so changing it requires a new index.
- **Validation.** Vectors must be finite and non-zero; they are L2-normalised.
  A different dimension is treated as a different model.
- **Batching.** `NEXA_EMBED_BATCH` texts per gateway call (default 32). One
  retry on gateway 5xx or connection errors; the gateway already retries its
  backend.
- **Cache.** Keyed by (fingerprint, SHA-256 of the prefixed text), stored in
  PostgreSQL (`knowledge.embedding_cache`). Queries are never cached: they are
  short, varied, and may contain personal wording.

## 3. Index versions

### 3.1 Lifecycle

```
           start_build              validation passes        activate
 (none) ───────────────► building ─────────────────► ready ──────────► active
                            │                          ▲                  │
                            │ validation fails         │ newer index      │
                            ▼                          │ activated        │
                          failed                       └──────────────────┘
                                                       retire (not active)
                                                 ready ──────────────────► retired
```

- Exactly one index is **active**; every query goes to it.
- Every write (an approved upload, a reprocessed run, a supersession) goes to
  the active index **and** to any index still building, so a build started
  before an upload does not miss it. Each target is written independently.
- **Activation** is one transaction that moves the `active` alias. The
  previous index becomes `ready` and is kept for rollback.
- **Rollback** first checks that the gateway still serves the old model, then
  catches the old index up on anything indexed since the switch, then
  activates it. It refuses if the old model is gone.
- **Retire** drops the index's table. The active index cannot be retired.
- The first upload on an empty store creates and activates a bootstrap index.

### 3.2 Validation (required before activation)

| Check | Rule |
|---|---|
| Completeness | Every active chunk of every active, approved source is present (`missing = 0`). |
| No stale content | Nothing from superseded, withdrawn or reprocessed-away chunks (`stale = 0`). |
| Self-retrieval | For a sample of up to 50 chunks, querying with the chunk's own text returns it in the top 5, for at least `NEXA_INDEX_SELF_RECALL_MIN` (0.8) of the sample. This catches a broken model, a wrong prefix scheme or a corrupt index. |
| Model | The gateway must currently serve the index's fingerprint. Checked even with `force`. |

Activation re-runs validation. `force: true` overrides the first three checks
(for an emergency, and it is audited), never the model check.

### 3.3 Query-time model check

Each query is embedded with the active index's logical model. If the gateway
reply's fingerprint differs from the index (for example, the gateway's
registry now maps `nexa-embed-default` to a new model), the query is refused
with **503** and the message tells the operator to build and activate a new
index. Comparing vectors from two models gives scores that look normal but are
meaningless, so refusing is the safe behaviour. The refusal is audited.

`GET /v1/knowledge/indexes/{id}?check_model=true` reports whether an index
still matches what the gateway serves.

## 4. pgvector store

| Object | Purpose |
|---|---|
| `knowledge.index_versions` | One row per index: status, fingerprint, build stats, validation report |
| `knowledge.index_alias` | The single `active` pointer |
| `knowledge.embedding_cache` | (fingerprint, text hash) to vector |
| `knowledge.vec_<index_id>` | One table per index: `vector(dim)` with an HNSW cosine index, the chunk text and metadata, and typed filter columns (approval, access class, scope, effective dates) |

- pgvector needs a fixed dimension to build an HNSW index, so each index
  version has its own table. Retiring an index drops its table cleanly.
- The access filter from M2-001 becomes a SQL `WHERE` clause on the typed
  columns, so filtering happens in the database.
- pgvector 0.6 (Ubuntu 24.04's package) filters *after* the HNSW scan, so
  filtered queries raise `hnsw.ef_search` to at least 4 × `top_k` (default 100).
  pgvector 0.8 adds iterative scans; the CI image uses 0.8.0.
- **The query must order by distance alone.** An extra sort key (for example a
  tie-breaker on `chunk_id`) stops PostgreSQL from using the HNSW index and
  falls back to a full scan. This was found during this work. Ties are now
  broken after the fetch, and a test checks the plan uses the index.
- Identifiers (schema, index ids) are validated against strict patterns
  before any DDL; values always go through query parameters.

## 5. API

All index endpoints need the new `knowledge.admin` permission, held by
`ai_engineer`, `admin` and `platform_admin`. Teachers cannot switch indexes.

| Endpoint | Purpose |
|---|---|
| `GET /v1/knowledge/indexes` | All index versions and the active one |
| `GET /v1/knowledge/indexes/{id}?check_model=` | One index; optionally compare with the gateway's current model |
| `POST /v1/knowledge/indexes` | Build a new index (`embed_model`, `activate_when_ready`, `note`). 202; runs in the background |
| `POST /v1/knowledge/indexes/{id}/validate` | Re-run validation |
| `POST /v1/knowledge/indexes/{id}/activate` | Activate a ready index (`force`) |
| `POST /v1/knowledge/indexes/rollback` | Return to the previous index |
| `DELETE /v1/knowledge/indexes/{id}` | Retire a non-active index |

`/health` now reports the active index and its fingerprint, and shows
`degraded` if the store is unreachable. Query responses report which index
answered (`x_nexa.index`).

## 6. Results

### 6.1 Real corpus in pgvector (offline embedder)

Run with `python v1/tools/knowledge_index_bench.py --corpus DIR --pg-dsn DSN --offline`
on the 72-file corpus from M2-002 (1,811 chunks), PostgreSQL 16 with pgvector
0.6.0, 768-dimension vectors from a deterministic local hashing embedder
(this sandbox cannot reach a real embedding model).

| Measure | Result |
|---|---|
| Build: chunks indexed, time, gateway calls | 1,811 in 5.4 s, 105 calls (batches of up to 32, per source, across 72 sources) |
| Validation | passed: 0 missing, 0 stale, self-recall@5 = 1.00 on 50 samples |
| Rebuild of the unchanged corpus | 0 document gateway calls, 1,811 cache hits |
| HNSW vs exact search, top-10 overlap (100 queries) | mean 0.981, worst 0.80 |
| Filtered student query latency | p50 1.9 ms, p95 2.4 ms |
| Query plan uses the HNSW index | yes |

These numbers measure the store and the index lifecycle. They say nothing
about semantic quality, because the offline embedder has no semantics.

### 6.2 On the DGX, with the real model (to run)

```
NEXA_KNOWLEDGE_GATEWAY_TOKEN=... python v1/tools/knowledge_index_bench.py \
    --corpus DIR --pg-dsn "$NEXA_KNOWLEDGE_PG_DSN" --compare-prefixes
```

This adds the 40-question gold set with nomic-embed-text through the gateway,
with prefixes and without. It confirms the prefix fix and gives the first
dense-retrieval baseline for M2-005 and M2-008. Record the result here.

## 7. Tests

`v1/tests/test_knowledge_indexing.py`, 23 tests. The store contract and the
index lifecycle run against both backends: the in-memory store always, and
PostgreSQL + pgvector when `NEXA_TEST_PG_DSN` is set. CI starts
`pgvector/pgvector:0.8.0-pg16` and fails if the pgvector tests skip. Covered:

- prefixes, normalisation, batching, cache, vector validation, model-change detection
- filter parity between Python and SQL
- store contract: upsert without duplicates, dimension check, filtered query,
  alias switching, retire, cache, persistence across reopen, HNSW plan used,
  unsafe identifiers rejected
- full blue/green lifecycle with a gateway model change mid-way; validation
  blocking an incomplete index; reprocess and supersede keeping the index exact;
  an unchanged rebuild using only the cache
- endpoints and RBAC, the 503 refusal, M1 raw ingest still working and idempotent

## 8. Operations

1. Install pgvector: `apt install postgresql-16-pgvector` (Ubuntu 24.04 ships 0.6.0; 0.5 is the minimum).
2. As an administrator: `psql -v svc_role=nexa_knowledge -f v1/knowledge/migrations/0002_vector_index.sql "$ADMIN_DSN"`.
3. In `/etc/nexa/knowledge.env`: `NEXA_KNOWLEDGE_STORE=pgvector` and
   `NEXA_KNOWLEDGE_PG_DSN`; the password goes in `.pgpass` or `PGPASSWORD`
   there, never in the repository. Set `NEXA_KNOWLEDGE_REINDEX_ON_START=false`.
4. Restart the service. The first approved upload, or
   `POST /v1/knowledge/sources/all/reindex`, creates the bootstrap index.
5. **Changing the embedding model:** point the gateway's logical model at the
   new backend. Queries return 503 until a matching index is active. Then
   `POST /v1/knowledge/indexes` with `activate_when_ready: true`. To keep
   serving during the change, register the new model under a second logical
   name, build with `embed_model` set to it, then switch the gateway mapping
   and activate.

## 9. Known limitations

- The registry (sources, jobs, chunks) is still SQLite. Its PostgreSQL adapter
  (DDL in `0001_knowledge_registry.sql`) is a separate step.
- Filtered HNSW on pgvector 0.6 can return fewer than `top_k` results when a
  filter excludes most of the corpus. The raised `ef_search` mitigates this;
  pgvector 0.8 iterative scans fix it.
- Index builds run inside the service process. A multi-worker deployment
  should run builds from one worker or a job runner.

## 10. Acceptance criteria

The ClickUp description for M2-004 was not available when this was written.
These are the criteria this work was built to; reconcile them with the ticket.

| Criterion | Evidence |
|---|---|
| Embeddings only through the Model Gateway, with model identity recorded | Section 2; fingerprint on every index |
| Persistent vector store per ADR-0002 | Section 4; pgvector tests in CI |
| Index versions with build, validate, activate, rollback, retire | Section 3; lifecycle test on both backends |
| No mixing of vectors from different models | Query-time 503; activation and rollback model checks; tests |
| Re-indexing is idempotent and has no retrieval gap | Upsert by chunk id; write-then-prune; tests |
| Rebuild of an unchanged corpus is cheap | Cache: 0 gateway calls for 1,811 chunks |
| Index quality checked before going live | Validation, section 3.2; HNSW vs exact overlap 0.981 |
