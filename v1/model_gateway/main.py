"""NEXA Model Gateway (M1 skeleton).

One OpenAI-compatible entry to every approved model. Callers use logical model
names (for example nexa-chat-default); the gateway resolves them to a backend
and version, applies the identity and policy hook, calls the runtime, and
stamps model, version, runtime and a trace id into every response.

v0 is not changed by this service. Later, v0 call sites move to this gateway
with no change to agents. See NEXA-ADR-0001 and NEXA-MIG-001 (M1).

Run:
    export NEXA_GATEWAY_TOKEN=... OPENAI_API_KEY=...
    uvicorn v1.model_gateway.main:app --host 127.0.0.1 --port 8100
"""
from __future__ import annotations
import time
import uuid

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import registry
from .schemas import ChatCompletionRequest, EmbeddingsRequest
from .identity import require_principal, Principal
from .observability import new_trace_id, log_event, Timer
from v1.observability import TraceMiddleware
from .backends.ollama_backend import OllamaBackend
from .backends.openai_backend import OpenAIBackend

app = FastAPI(title="NEXA Model Gateway", version="0.1.0")
app.add_middleware(TraceMiddleware, service="nexa-model-gateway")

# One backend instance per runtime. Add vLLM or NVIDIA NIM here later without
# touching callers (NEXA-ADR-0001).
_BACKENDS = {
    "ollama": OllamaBackend(),
    "openai": OpenAIBackend(),
}


def _backend(runtime: str):
    backend = _BACKENDS.get(runtime)
    if backend is None:
        raise HTTPException(status_code=500, detail=f"No backend for runtime {runtime}")
    return backend


def _nexa_trace(entry, trace_id: str, latency_ms: float) -> dict:
    return {
        "model": entry.logical,
        "model_version": entry.version,
        "runtime": entry.runtime,
        "trace_id": trace_id,
        "latency_ms": latency_ms,
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "nexa-model-gateway", "version": "0.1.0"}


@app.get("/v1/models")
async def list_models(principal: Principal = Depends(require_principal)):
    data = [
        {
            "id": e.logical,
            "object": "model",
            "runtime": e.runtime,
            "capability": e.capability,
            "version": e.version,
        }
        for e in registry.list_models()
    ]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
):
    entry = registry.resolve(req.model)
    if entry is None or entry.capability != "chat":
        raise HTTPException(status_code=404, detail=f"Unknown chat model {req.model}")

    temperature = req.temperature if req.temperature is not None else entry.default_temperature
    messages = [m.model_dump() for m in req.messages]
    trace_id = getattr(request.state, "trace_id", None) or new_trace_id()
    backend = _backend(entry.runtime)

    with Timer() as timer:
        try:
            result = await backend.chat(entry.backend_model, messages, temperature, req.max_tokens)
        except Exception as exc:  # upstream failure
            log_event(event="chat_error", trace_id=trace_id, model=entry.logical,
                      runtime=entry.runtime, actor=principal.subject, error=str(exc))
            raise HTTPException(status_code=502, detail="Upstream model error") from exc

    log_event(event="chat", trace_id=trace_id, model=entry.logical,
              model_version=entry.version, runtime=entry.runtime,
              latency_ms=timer.ms, actor=principal.subject)

    body = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": entry.logical,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": result["content"]},
            }
        ],
        "usage": result.get("usage", {}),
        "x_nexa": _nexa_trace(entry, trace_id, timer.ms),
    }
    return JSONResponse(body, headers={"X-NEXA-Trace-Id": trace_id})


@app.post("/v1/embeddings")
async def embeddings(
    req: EmbeddingsRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
):
    entry = registry.resolve(req.model)
    if entry is None or entry.capability != "embedding":
        raise HTTPException(status_code=404, detail=f"Unknown embedding model {req.model}")

    inputs = req.input if isinstance(req.input, list) else [req.input]
    trace_id = getattr(request.state, "trace_id", None) or new_trace_id()
    backend = _backend(entry.runtime)

    with Timer() as timer:
        try:
            vectors = await backend.embeddings(entry.backend_model, inputs)
        except Exception as exc:
            log_event(event="embed_error", trace_id=trace_id, model=entry.logical,
                      runtime=entry.runtime, actor=principal.subject, error=str(exc))
            raise HTTPException(status_code=502, detail="Upstream model error") from exc

    log_event(event="embed", trace_id=trace_id, model=entry.logical,
              model_version=entry.version, runtime=entry.runtime,
              latency_ms=timer.ms, actor=principal.subject, count=len(inputs))

    data = [
        {"object": "embedding", "index": i, "embedding": vec}
        for i, vec in enumerate(vectors)
    ]
    body = {
        "object": "list",
        "data": data,
        "model": entry.logical,
        "x_nexa": _nexa_trace(entry, trace_id, timer.ms),
    }
    return JSONResponse(body, headers={"X-NEXA-Trace-Id": trace_id})
