"""NEXA Knowledge Service (M1 skeleton). NEXA-ADR-0004.

Governed RAG boundary. Verifies the caller, enforces knowledge permissions,
embeds through the Model Gateway, and retrieves from a pluggable vector store.
The pgvector backend (NEXA-ADR-0002) and the full ingestion pipeline (OCR,
source authority, citations) are later steps and are not built here.

Run:
    export NEXA_IDENTITY_SECRET=...                 # shared, to verify callers
    export NEXA_KNOWLEDGE_GATEWAY_TOKEN=...         # service token for the gateway
    uvicorn v1.knowledge.main:app --host 127.0.0.1 --port 8103
"""
from __future__ import annotations
import time

from fastapi import FastAPI, Depends, HTTPException, Request

from v1.identity.principal import Principal
from v1.identity.rbac import can
from v1.observability import TraceMiddleware, new_trace_id, audit_event, metrics_endpoint
from v1.shared.config import preflight

from . import config, embeddings
from .auth import require_principal
from .store import get_store
from .schemas import (
    IngestRequest,
    IngestResponse,
    QueryRequest,
    QueryResponse,
    QueryResult,
)

app = FastAPI(title="NEXA Knowledge Service", version="0.1.0")
app.add_middleware(TraceMiddleware, service="nexa-knowledge")
app.add_route("/metrics", metrics_endpoint, include_in_schema=False)


@app.on_event("startup")
async def _startup():
    preflight("nexa-knowledge", ["NEXA_IDENTITY_SECRET", "NEXA_KNOWLEDGE_GATEWAY_TOKEN"])

# One process-wide store. For the in-memory backend this means ingested data
# lives only in this worker, which is fine for the skeleton.
_STORE = get_store()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "nexa-knowledge",
        "version": "0.1.0",
        "store": config.STORE_BACKEND,
        "embed_model": config.EMBED_MODEL,
    }


@app.post("/v1/knowledge/ingest", response_model=IngestResponse)
async def ingest(
    req: IngestRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
):
    trace_id = getattr(request.state, "trace_id", None) or new_trace_id()
    if not can(principal, "knowledge.ingest"):
        audit_event(action="knowledge.ingest", decision="deny",
                    principal=principal.subject, trace_id=trace_id)
        raise HTTPException(status_code=403, detail="Not permitted")
    audit_event(action="knowledge.ingest", decision="allow",
                principal=principal.subject, trace_id=trace_id)
    started = time.time()
    texts = [d.text for d in req.documents]
    try:
        vectors = await embeddings.embed(texts, trace_id)
    except embeddings.EmbeddingError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    items = [
        {"text": d.text, "metadata": d.metadata, "vector": vec}
        for d, vec in zip(req.documents, vectors)
    ]
    count = await _STORE.add(items)
    x_nexa = {
        "service": "nexa-knowledge",
        "op": "ingest",
        "trace_id": trace_id,
        "latency_ms": int((time.time() - started) * 1000),
        "principal": principal.subject,
        "count": count,
    }
    return IngestResponse(ingested=count, x_nexa=x_nexa)


@app.post("/v1/knowledge/query", response_model=QueryResponse)
async def query(
    req: QueryRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
):
    trace_id = getattr(request.state, "trace_id", None) or new_trace_id()
    if not can(principal, "knowledge.query"):
        audit_event(action="knowledge.query", decision="deny",
                    principal=principal.subject, trace_id=trace_id)
        raise HTTPException(status_code=403, detail="Not permitted")
    started = time.time()
    top_k = req.top_k or config.DEFAULT_TOP_K
    try:
        vectors = await embeddings.embed([req.query], trace_id)
    except embeddings.EmbeddingError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not vectors:
        raise HTTPException(status_code=502, detail="Empty query embedding")
    rows = await _STORE.query(vectors[0], top_k)
    results = [
        QueryResult(text=r["text"], score=r["score"], metadata=r.get("metadata", {}))
        for r in rows
    ]
    x_nexa = {
        "service": "nexa-knowledge",
        "op": "query",
        "trace_id": trace_id,
        "latency_ms": int((time.time() - started) * 1000),
        "principal": principal.subject,
        "returned": len(results),
    }
    return QueryResponse(query=req.query, results=results, x_nexa=x_nexa)
