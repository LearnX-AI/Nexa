# NEXA Knowledge Service

The governed RAG boundary for v1 (NEXA-ADR-0004). It replaces the in-monolith
Chroma path from v0. Callers never touch a vector store or an embedding SDK
directly; they go through this service, which embeds through the Model Gateway
and retrieves from a pluggable store.

M1 delivered the skeleton (auth, embeddings via the gateway, in-memory store).
M2 adds the knowledge layer:

| Task | What | Code | Standard |
|---|---|---|---|
| NEXA-M2-001 | Source metadata contract, authority and conflict rules, access rules, PNG curriculum recognisers | `metadata.py`, `authority.py`, `access.py`, `curriculum.py` | `docs/knowledge/NEXA-M2-001-curriculum-source-metadata-standard.md` |
| NEXA-M2-002 | Controlled ingestion: validation, PDF/DOCX/PPTX/LMS/image extraction, OCR, idempotent versioned registry, jobs and retry | `ingestion/` | `docs/knowledge/NEXA-M2-002-ingestion-pipeline.md` |
| NEXA-M2-003 | Normalization, structure recovery, semantic chunking, v0 vs v1 benchmark | `processing/`, `benchmark/` | `docs/knowledge/NEXA-M2-003-semantic-chunking.md` |
| NEXA-M2-004 | Embedding service (prefixes, model fingerprint, cache), versioned indexes with blue/green activation and rollback, pgvector store | `embeddings.py`, `indexing.py`, `store.py`, `pgstore.py` | `docs/knowledge/NEXA-M2-004-embedding-service-index-versioning.md` |

## Request flow

1. A caller presents a signed NEXA identity token (NEXA-ADR-0005).
2. The service verifies it and enforces the permission: `knowledge.query` to
   search and read sources, `knowledge.ingest` to upload, manage jobs and
   review chunks.
3. Uploads run validate, extract, scan, metadata, chunk in the background;
   approved sources are then embedded through the Model Gateway (`nexa-embed-default`).
4. Queries are embedded the same way and filtered by approval, effective date,
   access class and scope before ranking. Each result carries a citation.

## Endpoints

- `GET  /health`  liveness, store, embed model, schema and pipeline versions; no auth.
- `POST /v1/knowledge/sources`  upload a file with a JSON manifest; returns a job.
- `POST /v1/knowledge/lms-items`  push an EduNeX or LMS item (HTML or text).
- `GET  /v1/knowledge/jobs`, `GET /v1/knowledge/jobs/{id}`  job state, stage, errors, warnings.
- `POST /v1/knowledge/jobs/{id}/retry`  retry, optionally with a `metadata_patch` (approval, fixes).
- `GET  /v1/knowledge/sources`, `GET /v1/knowledge/sources/{version_id}`  versions and runs.
- `GET  /v1/knowledge/sources/{version_id}/chunks`  chunk review.
- `POST /v1/knowledge/sources/{version_id|all}/reindex`  re-embed approved sources.
- `GET  /v1/knowledge/schema/source-metadata`  the M2-001 JSON Schema.
- `POST /v1/knowledge/query`  retrieve, access-filtered, with citations.
- `POST /v1/knowledge/ingest`  M1 raw-text ingest, kept for compatibility.
- `GET/POST /v1/knowledge/indexes`, `.../{id}`, `.../{id}/validate`, `.../{id}/activate`,
  `POST .../indexes/rollback`, `DELETE .../{id}`  index versions (`knowledge.admin`).

## Uploading a source

```
curl -H "Authorization: Bearer $TOKEN" \
  -F file=@STEMChem-Term1-Syllubus-Gr12.pdf \
  -F 'manifest={"metadata": {"approval_status": "approved", "approved_by": "content-team",
                             "effective_from": "2023-02-10",
                             "source_uri": "repo://LearnX-AI/nexa/nexa-v1-core/STEMChem-Term1-Syllubus-Gr12.pdf"}}' \
  http://127.0.0.1:8103/v1/knowledge/sources
```

Subject, grade, term, source type, publisher and access class are drafted from
the PNG file name; the manifest supplies the governance fields and wins over
anything inferred. Without `approval_status: approved` the source is chunked
for review but not indexed.

## Config (environment)

See `.env.example`. The main settings:

- `NEXA_IDENTITY_SECRET`, `NEXA_KNOWLEDGE_GATEWAY_TOKEN`  required.
- `NEXA_KNOWLEDGE_REGISTRY`  `memory` or `sqlite:///path`.
- `NEXA_KNOWLEDGE_BLOB_DIR`  content-addressed originals.
- `NEXA_KNOWLEDGE_AV`, `NEXA_KNOWLEDGE_REQUIRE_AV`  ClamAV; require it outside DEV.
- `NEXA_KNOWLEDGE_OCR*`  tesseract settings (needs `tesseract-ocr` and `tesseract-ocr-eng`).
- `NEXA_KNOWLEDGE_IMAGE_UPLOADS`  `auto` (on with Pillow 13+ only), `off`, `force`.
- `NEXA_KNOWLEDGE_REINDEX_ON_START`  re-embed approved sources at startup.
- `NEXA_KNOWLEDGE_STORE`  `memory` (tests) or `pgvector`; `NEXA_KNOWLEDGE_PG_DSN` for the latter.
- `NEXA_EMBED_PREFIX_SCHEME`, `NEXA_EMBED_BATCH`, `NEXA_EMBED_CACHE`  embedding service.
- `NEXA_INDEX_SELF_RECALL_MIN`, `NEXA_HNSW_*`  index validation and HNSW tuning.

## Tools

- `python v1/tools/knowledge_corpus.py --out DIR`  ingest the real curriculum corpus,
  prove idempotency, write reports and chunks.
- `python v1/tools/knowledge_benchmark.py --corpus DIR`  v0 against v1 chunking benchmark.
- `python v1/tools/knowledge_index_bench.py --corpus DIR --pg-dsn DSN [--offline] [--compare-prefixes]`
  index build, cache, HNSW quality and latency; gold-set recall through the gateway.

## Not built yet, by design

Hybrid vector and full-text retrieval and reranking with claim-class authority
weighting (M2-005), the grounding contract (M2-006), scheduled EduNeX pull
synchronisation (M2-007), the full evaluation set (M2-008), the school edge
cache (M2-009), the registry's PostgreSQL adapter, and migration of the v0
Chroma store (M2-010).
