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
    IndexActivateRequest,
    IndexBuildRequest,
    JobRetryRequest,
    LmsItemRequest,
    QueryRequest,
    QueryResponse,
    QueryResult,
)
from .indexing import IndexError_, IndexManager
from .store import get_store

app = FastAPI(title="NEXA Knowledge Service", version=__version__)
app.add_middleware(TraceMiddleware, service="nexa-knowledge")
app.add_route("/metrics", metrics_endpoint, include_in_schema=False)


# One process-wide store, pipeline and index manager. With the in-memory store
# vectors live only in this worker; with pgvector they persist. The registry and
# originals persist when NEXA_KNOWLEDGE_REGISTRY and NEXA_KNOWLEDGE_BLOB_DIR
# point at disk.
_STORE = get_store()
PIPE = Pipeline()
INDEX = IndexManager(_STORE, PIPE.registry)


@app.on_event("startup")
async def _startup():
    required = ["NEXA_IDENTITY_SECRET", "NEXA_KNOWLEDGE_GATEWAY_TOKEN"]
    if config.STORE_BACKEND == "pgvector":
        required.append("NEXA_KNOWLEDGE_PG_DSN")
    preflight("nexa-knowledge", required)
    await INDEX.store.open()
    if config.REINDEX_ON_START:
        import asyncio
        asyncio.get_running_loop().create_task(reindex_all())


@app.on_event("shutdown")
async def _shutdown():
    await INDEX.store.close()


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
    try:
        active = await INDEX.active()
        index = {"active_index": active.index_id, "embed_fingerprint": active.fingerprint} if active \
            else {"active_index": None}
    except Exception as exc:  # the store being down must show, not crash health
        index = {"active_index": None, "store_error": type(exc).__name__}
    return {
        "status": "ok" if "store_error" not in index else "degraded",
        "service": "nexa-knowledge",
        "version": __version__,
        "store": config.STORE_BACKEND,
        "embed_model": config.EMBED_MODEL,
        **index,
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
    try:
        count = await INDEX.add_raw([{"text": d.text, "metadata": d.metadata} for d in req.documents], trace_id)
    except embeddings.EmbeddingError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
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
        rows, rec = await INDEX.query(req.query, top_k, where=retrieval_filter(principal), trace_id=trace_id)
    except embeddings.ModelChanged as exc:
        audit_event(action="knowledge.query", decision="refuse", principal=principal.subject,
                    trace_id=trace_id, reason="embedding_model_changed")
        raise HTTPException(status_code=503, detail=str(exc))
    except embeddings.EmbeddingError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
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
        "index": rec.index_id if rec else None,
    }
    return QueryResponse(query=req.query, results=results, x_nexa=x_nexa)


# ------------------------------------------------------------------ M2 ingestion

async def index_version(version_id: str, trace_id: Optional[str] = None) -> int:
    """Embed one approved source version into the active index (and any index
    still building). Raises EmbeddingError."""
    out = await INDEX.index_version(version_id, trace_id)
    return out.get("indexed", 0)


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
    """Re-embed every active, approved source into the active index. Needed after
    a restart with the in-memory store; cached vectors make it cheap."""
    try:
        return await INDEX.reindex_all(trace_id=trace_id)
    except embeddings.EmbeddingError as exc:
        return {"indexed_sources": 0, "failed_sources": -1, "error": str(exc)[:300]}


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


# ------------------------------------------------------------------ M2-004 index versions

def _index_error(exc: IndexError_) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


@app.get("/v1/knowledge/indexes")
async def list_indexes(request: Request, principal: Principal = Depends(require_principal)):
    _require(principal, "knowledge.admin", "knowledge.indexes.list", _trace(request))
    active = await INDEX.store.active_index_id()
    return {"active_index": active,
            "indexes": [r.to_dict() for r in await INDEX.store.list_indexes()]}


@app.get("/v1/knowledge/indexes/{index_id}")
async def get_index(index_id: str, request: Request, check_model: bool = False,
                    principal: Principal = Depends(require_principal)):
    """One index. With check_model=true, also asks the gateway which model it
    serves now and reports whether this index still matches it."""
    trace_id = _trace(request)
    _require(principal, "knowledge.admin", "knowledge.indexes.get", trace_id)
    rec = await INDEX.store.get_index(index_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="index not found")
    out = rec.to_dict()
    if check_model:
        try:
            now = await INDEX.embedder(rec.logical).describe(trace_id)
            out["gateway_fingerprint"] = now.fingerprint
            out["model_matches"] = now.fingerprint == rec.fingerprint
        except embeddings.EmbeddingError as exc:
            out["gateway_error"] = str(exc)[:300]
    return out


async def _build(index_id: str, activate: bool, trace_id: str) -> None:
    await INDEX.run_build(index_id, activate=activate, trace_id=trace_id)


@app.post("/v1/knowledge/indexes", status_code=202)
async def build_index(request: Request, background: BackgroundTasks, body: Optional[IndexBuildRequest] = None,
                      principal: Principal = Depends(require_principal)):
    """Build a new index from every active, approved source, beside the active
    one. It is validated when the build ends; activate it explicitly or set
    activate_when_ready."""
    trace_id = _trace(request)
    body = body or IndexBuildRequest()
    _require(principal, "knowledge.admin", "knowledge.indexes.build", trace_id,
             model=body.embed_model or config.EMBED_MODEL)
    try:
        rec = await INDEX.start_build(body.embed_model, note=body.note, trace_id=trace_id)
    except IndexError_ as exc:
        raise _index_error(exc)
    background.add_task(_build, rec.index_id, body.activate_when_ready, trace_id)
    return rec.to_dict()


@app.post("/v1/knowledge/indexes/{index_id}/activate")
async def activate_index(index_id: str, request: Request, body: Optional[IndexActivateRequest] = None,
                         principal: Principal = Depends(require_principal)):
    trace_id = _trace(request)
    force = bool(body and body.force)
    _require(principal, "knowledge.admin", "knowledge.indexes.activate", trace_id, index=index_id, force=force)
    try:
        return (await INDEX.activate(index_id, force=force, trace_id=trace_id)).to_dict()
    except IndexError_ as exc:
        raise _index_error(exc)


@app.post("/v1/knowledge/indexes/rollback")
async def rollback_index(request: Request, principal: Principal = Depends(require_principal)):
    trace_id = _trace(request)
    _require(principal, "knowledge.admin", "knowledge.indexes.rollback", trace_id)
    try:
        return (await INDEX.rollback(trace_id=trace_id)).to_dict()
    except IndexError_ as exc:
        raise _index_error(exc)


@app.post("/v1/knowledge/indexes/{index_id}/validate")
async def validate_index(index_id: str, request: Request, principal: Principal = Depends(require_principal)):
    trace_id = _trace(request)
    _require(principal, "knowledge.admin", "knowledge.indexes.validate", trace_id, index=index_id)
    if await INDEX.store.get_index(index_id) is None:
        raise HTTPException(status_code=404, detail="index not found")
    try:
        result = await INDEX.validate(index_id, trace_id)
    except embeddings.ModelChanged as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except embeddings.EmbeddingError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    await INDEX.store.update_index(index_id, validation=result)
    return result


@app.delete("/v1/knowledge/indexes/{index_id}")
async def retire_index(index_id: str, request: Request, principal: Principal = Depends(require_principal)):
    trace_id = _trace(request)
    _require(principal, "knowledge.admin", "knowledge.indexes.retire", trace_id, index=index_id)
    try:
        return (await INDEX.retire(index_id)).to_dict()
    except IndexError_ as exc:
        raise _index_error(exc)
