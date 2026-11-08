"""NEXA Knowledge Service. NEXA-ADR-0004.

M1 skeleton plus the M2 knowledge layer:
- NEXA-M2-001 source metadata contract and authority rules (metadata.py, authority.py)
- NEXA-M2-002 controlled ingestion, parsing and OCR (ingestion/)
- NEXA-M2-003 normalization and semantic chunking (processing/)

Governed RAG boundary. Verifies the caller, enforces knowledge permissions,
embeds through the Model Gateway, and retrieves from a pluggable vector store.

Run:
    export NEXA_IDENTITY_SECRET=...                 # shared, to verify callers
    export NEXA_KNOWLEDGE_GATEWAY_TOKEN=...         # service token for the gateway
    export NEXA_KNOWLEDGE_REGISTRY=sqlite:////var/lib/nexa/knowledge/registry.db
    export NEXA_KNOWLEDGE_BLOB_DIR=/var/lib/nexa/knowledge/originals
    uvicorn v1.knowledge.main:app --host 127.0.0.1 --port 8103
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from pydantic import ValidationError as PydanticValidationError
from starlette.concurrency import run_in_threadpool

from v1.identity.principal import Principal
from v1.identity.rbac import can
from v1.observability import TraceMiddleware, audit_event, metrics_endpoint, new_trace_id
from v1.shared.config import preflight

from . import __version__, config, embeddings
from .access import retrieval_filter
from .auth import require_principal
from .ingestion import PIPELINE_VERSION
from .ingestion.pipeline import Pipeline
from .ingestion.validation import ValidationError
from .metadata import SCHEMA_VERSION, json_schema
from .processing.chunking import CHUNKER_VERSION, ChunkingConfig
from .processing.normalize import NORMALIZER_VERSION
from .schemas import (
    IngestManifest,
    IngestRequest,
    IngestResponse,
    JobRetryRequest,
    LmsItemRequest,
    QueryRequest,
    QueryResponse,
    QueryResult,
)
from .store import get_store

app = FastAPI(title="NEXA Knowledge Service", version=__version__)
app.add_middleware(TraceMiddleware, service="nexa-knowledge")
app.add_route("/metrics", metrics_endpoint, include_in_schema=False)


@app.on_event("startup")
async def _startup():
    preflight("nexa-knowledge", ["NEXA_IDENTITY_SECRET", "NEXA_KNOWLEDGE_GATEWAY_TOKEN"])
    if config.REINDEX_ON_START:
        import asyncio
        asyncio.get_running_loop().create_task(reindex_all())

# One process-wide store and pipeline. With the in-memory store, ingested
# vectors live only in this worker; the registry and originals persist when
# NEXA_KNOWLEDGE_REGISTRY and NEXA_KNOWLEDGE_BLOB_DIR point at disk.
_STORE = get_store()
PIPE = Pipeline()

EMBED_BATCH = 32


def _trace(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or new_trace_id()


def _require(principal: Principal, perm: str, action: str, trace_id: str, **extra: Any) -> None:
    if not can(principal, perm):
        audit_event(action=action, decision="deny", principal=principal.subject, trace_id=trace_id, **extra)
        raise HTTPException(status_code=403, detail="Not permitted")
    audit_event(action=action, decision="allow", principal=principal.subject, trace_id=trace_id, **extra)


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    keys = ("job_id", "state", "stage", "file_name", "mime_type", "checksum", "version_id", "run_id",
            "attempts", "retryable", "error_code", "error_detail", "warnings", "created_at", "updated_at")
    return {k: job.get(k) for k in keys}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "nexa-knowledge",
        "version": __version__,
        "store": config.STORE_BACKEND,
        "embed_model": config.EMBED_MODEL,
        "metadata_schema": SCHEMA_VERSION,
        "pipeline": PIPELINE_VERSION,
        "normalizer": NORMALIZER_VERSION,
        "chunker": CHUNKER_VERSION,
    }


# ------------------------------------------------------------------ M1 endpoints

@app.post("/v1/knowledge/ingest", response_model=IngestResponse)
async def ingest(
    req: IngestRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
):
    """Raw text ingest (M1). Kept for compatibility; curriculum sources go
    through /v1/knowledge/sources so they get the M2 metadata contract."""
    trace_id = _trace(request)
    _require(principal, "knowledge.ingest", "knowledge.ingest", trace_id)
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
    trace_id = _trace(request)
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
    rows = await _STORE.query(vectors[0], top_k, where=retrieval_filter(principal))
    results = [
        QueryResult(text=r["text"], score=r["score"], metadata=r.get("metadata", {}),
                    citation=(r.get("metadata") or {}).get("citation"))
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


# ------------------------------------------------------------------ M2 ingestion

def _store_metadata(c: dict[str, Any]) -> dict[str, Any]:
    """Flat metadata stored with each vector: the source contract plus the
    chunk's own provenance, so a hit can be cited and filtered without a join."""
    loc, cur = c["locator"], c["curriculum"]
    md = dict(c["source"])
    md.update({
        "chunk_id": c["chunk_id"], "ordinal": c["ordinal"], "citation": c["citation"],
        "page_start": loc.get("page_start"), "page_end": loc.get("page_end"), "page_label": loc.get("page_label"),
        "page_basis": loc.get("page_basis"), "section_path": " > ".join(loc.get("section_path") or []),
        "anchor": loc.get("anchor"),
        "thema": cur.get("thema") or md.get("thema"), "module_code": cur.get("module_code") or md.get("module_code"),
        "module_title": cur.get("module_title"), "topic_code": cur.get("topic_code"),
        "topic_title": cur.get("topic_title"), "outcome_codes": ",".join(cur.get("outcome_codes") or []),
        "prev_chunk_id": c.get("prev_chunk_id"), "next_chunk_id": c.get("next_chunk_id"),
        "low_confidence": c["extraction"]["low_confidence"],
        "chunker_version": c["chunking"]["chunker_version"], "chunk_config_hash": c["chunking"]["config_hash"],
        "run_id": c["extraction"]["run_id"],
    })
    return md


async def index_version(version_id: str, trace_id: Optional[str] = None) -> int:
    """Embed one source version's active chunks through the Model Gateway and
    replace them in the vector store, removing superseded versions of the same
    source. Raises EmbeddingError; on failure nothing of this version remains
    half-indexed. Returns the number of chunks indexed."""
    reg = PIPE.registry
    src = reg.get_source(version_id)
    if src is None or src["status"] != "active" or src["metadata"].get("approval_status") != "approved":
        return 0
    for v in reg.versions_of(src["source_id"]):
        if v["status"] != "active":
            await _STORE.delete_version(v["version_id"])
    await _STORE.delete_version(version_id)
    chunks = reg.chunks_of(version_id)
    try:
        for i in range(0, len(chunks), EMBED_BATCH):
            batch = chunks[i:i + EMBED_BATCH]
            vectors = await embeddings.embed([c["embedding_text"] for c in batch], trace_id)
            if len(vectors) != len(batch):
                raise embeddings.EmbeddingError("embedding count mismatch")
            await _STORE.add([{"text": c["text"], "vector": v, "metadata": _store_metadata(c)}
                              for c, v in zip(batch, vectors)])
            reg.mark_embedded([c["chunk_id"] for c in batch])
    except embeddings.EmbeddingError:
        await _STORE.delete_version(version_id)
        raise
    return len(chunks)


async def index_job(job_id: str, trace_id: Optional[str] = None) -> dict[str, Any]:
    """Index a job's source version. Only approved sources are indexed."""
    reg = PIPE.registry
    job = reg.get_job(job_id)
    if not job or job["stage"] != "index" or not job.get("version_id"):
        return job or {}
    run = reg.get_run(job["run_id"]) if job.get("run_id") else None
    final_state = "partial" if run and run["status"] == "partial" else "succeeded"
    src = reg.get_source(job["version_id"])
    if src is None:
        return reg.update_job(job_id, state="failed", error_code="source_missing", retryable=False)
    if src["metadata"].get("approval_status") != "approved":
        warnings = [w for w in job["warnings"] if not w.startswith("not indexed")]
        warnings.append("not indexed: source is not approved; retry with an approval metadata patch")
        return reg.update_job(job_id, state=final_state, warnings=warnings)
    try:
        await index_version(job["version_id"], trace_id)
    except embeddings.EmbeddingError as exc:
        return reg.update_job(job_id, state="failed", error_code="index_error", error_detail=str(exc)[:500],
                              retryable=True)
    return reg.update_job(job_id, state=final_state, stage="done", error_code=None, error_detail=None)


async def reindex_all(trace_id: Optional[str] = None) -> dict[str, int]:
    """Re-embed every active, approved source. Used after a restart while the
    vector store is the in-memory M1 store, and after an embedding model change."""
    done = failed = 0
    for s in PIPE.registry.list_sources("active"):
        try:
            if await index_version(s["version_id"], trace_id):
                done += 1
        except embeddings.EmbeddingError:
            failed += 1
    return {"indexed_sources": done, "failed_sources": failed}


async def _process_and_index(job_id: str, trace_id: str) -> None:
    job = await run_in_threadpool(PIPE.process, job_id)
    if job.get("stage") == "index" and job.get("state") in ("succeeded", "partial", "queued"):
        await index_job(job_id, trace_id)


def _parse_manifest(raw: Optional[str]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        m = IngestManifest(**json.loads(raw))
        if m.chunking:
            ChunkingConfig(**m.chunking)
    except (json.JSONDecodeError, PydanticValidationError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid manifest: {exc}")
    return m.model_dump(exclude_none=True)


@app.post("/v1/knowledge/sources", status_code=202)
async def upload_source(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    manifest: Optional[str] = Form(None),
    principal: Principal = Depends(require_principal),
):
    """Upload a curriculum file. manifest is JSON: {"metadata": {...}, "chunking": {...}}.
    Returns the job; processing continues in the background."""
    trace_id = _trace(request)
    _require(principal, "knowledge.ingest", "knowledge.source.upload", trace_id, file=file.filename)
    man = _parse_manifest(manifest)
    data = await file.read(config.MAX_FILE_BYTES + 1)
    if len(data) > config.MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="file exceeds size limit")
    job, created = await run_in_threadpool(PIPE.submit, file.filename or "upload", data, man, principal.subject)
    if created and job["state"] == "queued":
        background.add_task(_process_and_index, job["job_id"], trace_id)
    return {"created": created, "job": _job_view(job)}


@app.post("/v1/knowledge/lms-items", status_code=202)
async def push_lms_item(
    req: LmsItemRequest,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(require_principal),
):
    """EduNeX or LMS content pushed as HTML or text with its LMS reference."""
    trace_id = _trace(request)
    _require(principal, "knowledge.ingest", "knowledge.lms.push", trace_id, item=req.lms.item_id)
    try:
        job, created = await run_in_threadpool(PIPE.submit_lms, req.model_dump(exclude_none=True), principal.subject)
    except ValidationError as exc:
        raise HTTPException(status_code=413 if exc.code == "too_large" else 422, detail=exc.detail)
    if created and job["state"] == "queued":
        background.add_task(_process_and_index, job["job_id"], trace_id)
    return {"created": created, "job": _job_view(job)}


@app.get("/v1/knowledge/jobs")
async def list_jobs(request: Request, state: Optional[str] = None, limit: int = 100,
                    principal: Principal = Depends(require_principal)):
    _require(principal, "knowledge.ingest", "knowledge.jobs.list", _trace(request))
    return {"jobs": [_job_view(j) for j in PIPE.registry.list_jobs(state, min(max(limit, 1), 500))]}


@app.get("/v1/knowledge/jobs/{job_id}")
async def get_job(job_id: str, request: Request, principal: Principal = Depends(require_principal)):
    _require(principal, "knowledge.ingest", "knowledge.jobs.get", _trace(request))
    job = PIPE.registry.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_view(job)


@app.post("/v1/knowledge/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: str, request: Request, background: BackgroundTasks,
                    body: Optional[JobRetryRequest] = None,
                    principal: Principal = Depends(require_principal)):
    trace_id = _trace(request)
    _require(principal, "knowledge.ingest", "knowledge.jobs.retry", trace_id, job=job_id)
    if not PIPE.registry.get_job(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    patch = body.metadata_patch if body else None
    job = await run_in_threadpool(PIPE.retry, job_id, patch)
    if job["stage"] == "index" and job["state"] in ("queued", "succeeded", "partial"):
        background.add_task(index_job, job_id, trace_id)
    return _job_view(job)


@app.get("/v1/knowledge/sources")
async def list_sources(request: Request, status: Optional[str] = "active",
                       principal: Principal = Depends(require_principal)):
    _require(principal, "knowledge.query", "knowledge.sources.list", _trace(request))
    rows = PIPE.registry.list_sources(status)
    return {"sources": [{"version_id": r["version_id"], "source_id": r["source_id"], "status": r["status"],
                         "title": r["metadata"].get("title"), "source_type": r["metadata"].get("source_type"),
                         "subject": r["metadata"].get("subject"), "grade_levels": r["metadata"].get("grade_levels"),
                         "approval_status": r["metadata"].get("approval_status"),
                         "access_classification": r["metadata"].get("access_classification")} for r in rows]}


@app.get("/v1/knowledge/sources/{version_id}")
async def get_source(version_id: str, request: Request, principal: Principal = Depends(require_principal)):
    _require(principal, "knowledge.query", "knowledge.sources.get", _trace(request))
    src = PIPE.registry.get_source(version_id)
    if not src:
        raise HTTPException(status_code=404, detail="source version not found")
    runs = PIPE.registry.runs_of(version_id)
    return {"source": src, "runs": runs}


@app.post("/v1/knowledge/sources/{version_id}/reindex", status_code=202)
async def reindex_source(version_id: str, request: Request, background: BackgroundTasks,
                         principal: Principal = Depends(require_principal)):
    """Re-embed one approved, active source version (or every one with
    version_id "all") through the Model Gateway."""
    trace_id = _trace(request)
    _require(principal, "knowledge.ingest", "knowledge.reindex", trace_id, version=version_id)
    if version_id == "all":
        background.add_task(reindex_all, trace_id)
        return {"queued": "all"}
    src = PIPE.registry.get_source(version_id)
    if not src:
        raise HTTPException(status_code=404, detail="source version not found")
    if src["status"] != "active" or src["metadata"].get("approval_status") != "approved":
        raise HTTPException(status_code=409, detail="only active, approved sources are indexed")
    background.add_task(index_version, version_id, trace_id)
    return {"queued": version_id}


@app.get("/v1/knowledge/sources/{version_id}/chunks")
async def get_source_chunks(version_id: str, request: Request, active: bool = True,
                            principal: Principal = Depends(require_principal)):
    """Chunk review for content staff (chunk quality review, M2-003)."""
    _require(principal, "knowledge.ingest", "knowledge.chunks.review", _trace(request))
    if not PIPE.registry.get_source(version_id):
        raise HTTPException(status_code=404, detail="source version not found")
    return {"chunks": PIPE.registry.chunks_of(version_id, active_only=active)}


@app.get("/v1/knowledge/schema/source-metadata")
async def source_metadata_schema(principal: Principal = Depends(require_principal)):
    return json_schema()
