# NEXA Knowledge Service (M1 skeleton)

The governed RAG boundary for v1 (NEXA-ADR-0004). It replaces the in-monolith
Chroma path from v0. Callers never touch a vector store or an embedding SDK
directly; they go through this service, which embeds through the Model Gateway
and retrieves from a pluggable store.

## Request flow

1. A caller presents a signed NEXA identity token (NEXA-ADR-0005).
2. The service verifies it and enforces the permission: `knowledge.query` to
   search, `knowledge.ingest` to add documents.
3. Text is embedded through the Model Gateway on 8100 using the service's own
   token and the logical model `nexa-embed-default`.
4. The vector goes to the store backend for add or similarity query.

No direct Ollama, OpenAI or Chroma access: embeddings go through the gateway and
the store is behind an interface.

## Endpoints

- `GET  /health`  liveness, store backend and embed model, no auth.
- `POST /v1/knowledge/ingest`  add documents, requires `knowledge.ingest`.
- `POST /v1/knowledge/query`  retrieve, requires `knowledge.query`.

## Config (environment)

- `NEXA_IDENTITY_SECRET`  shared signing secret, required to verify callers.
- `NEXA_KNOWLEDGE_GATEWAY_TOKEN`  service token the service presents to the gateway.
- `NEXA_EMBED_MODEL`  logical embedding model, default `nexa-embed-default`.
- `NEXA_KNOWLEDGE_STORE`  `memory` (default) or `pgvector` (deferred).
- `NEXA_KNOWLEDGE_HOST` / `NEXA_KNOWLEDGE_PORT`  default `127.0.0.1:8103`.

## Store backends

- `memory`: in-process, non-persistent, not shared across workers. For the
  skeleton and tests.
- `pgvector`: deferred to the NEXA-ADR-0002 implementation step. Selecting it
  raises until it is built.

## Not in this skeleton, by design

The pgvector backend, the full ingestion pipeline (document extraction, OCR,
chunking strategy, source authority and ranking, citations), re-index jobs, and
migration of the existing Chroma store. Those are later steps. Do not add future
phase work here (NEXA-DEV-001 section 15).
