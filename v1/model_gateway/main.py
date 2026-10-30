"""NEXA Model Gateway (M1).

One OpenAI-compatible entry to every approved model. Callers use logical model
names (for example nexa-chat-default); the gateway resolves them to a backend
and version, applies the identity and policy hook, calls the runtime under the
retry and circuit-breaker policy, and stamps model, version, runtime and a trace
id into every response. Supports streaming (server-sent events) and records
token, latency and error telemetry per model.

Runtimes: ollama (v0 LLaMA), openai, vllm, nim. Adding a model, or moving a
logical name to another runtime, is a registry config change (NEXA_MODEL_CONFIG)
with no change to callers. See NEXA-ADR-0001 and NEXA-MIG-001 (M1).

Run:
    export NEXA_IDENTITY_SECRET=... OPENAI_API_KEY=...
    uvicorn v1.model_gateway.main:app --host 127.0.0.1 --port 8100
"""
from __future__ import annotations
import json
import time
import uuid

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import config, registry
from .schemas import ChatCompletionRequest, EmbeddingsRequest
from .identity import require_principal, Principal
from .observability import new_trace_id, log_event, Timer
from v1.observability import TraceMiddleware, metrics, metrics_endpoint
from v1.shared.config import preflight
from .backends.base import (
    CircuitOpenError, breaker_for, guarded, guarded_stream,
)
from .backends.ollama_backend import OllamaBackend
from .backends.openai_backend import OpenAIBackend
from .backends.vllm_backend import VLLMBackend
from .backends.nim_backend import NIMBackend

app = FastAPI(title="NEXA Model Gateway", version="0.2.0")
app.add_middleware(TraceMiddleware, service="nexa-model-gateway")
app.add_route("/metrics", metrics_endpoint, include_in_schema=False)


@app.on_event("startup")
async def _startup():
    preflight("nexa-model-gateway", ["NEXA_IDENTITY_SECRET"])
    # Load the registry from NEXA_MODEL_CONFIG when set. An invalid file raises
    # RegistryError, so the service refuses to start on bad model config.
    registry.reload(config.MODEL_CONFIG or None)


# One backend instance per runtime. A new runtime is one line here plus
# registry entries; callers are untouched (NEXA-ADR-0001).
_BACKENDS = {
    "ollama": OllamaBackend(),
    "openai": OpenAIBackend(),
    "vllm": VLLMBackend(),
    "nim": NIMBackend(),
}


def _backend(runtime: str):
    backend = _BACKENDS.get(runtime)
    if backend is None:
        raise HTTPException(status_code=500, detail=f"No backend for runtime {runtime}")
    return backend


def _nexa_trace(entry, trace_id: str, latency_ms: float, usage: dict | None = None) -> dict:
    block = {
        "model": entry.logical,
        "model_version": entry.version,
        "runtime": entry.runtime,
        "trace_id": trace_id,
        "latency_ms": latency_ms,
    }
    if usage:
        block["usage"] = usage
    return block


def _tokens(usage: dict) -> tuple[int, int]:
    return int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)


def _record(entry, outcome: str, latency_ms: float, usage: dict | None = None) -> None:
    """Per-model telemetry: calls, errors, latency and tokens."""
    metrics.inc("nexa_model_requests_total", model=entry.logical,
                runtime=entry.runtime, outcome=outcome)
    metrics.observe("nexa_model_latency_ms", latency_ms,
                    model=entry.logical, runtime=entry.runtime)
    if usage:
        prompt, completion = _tokens(usage)
        metrics.inc("nexa_model_tokens_total", prompt, model=entry.logical, kind="prompt")
        metrics.inc("nexa_model_tokens_total", completion, model=entry.logical, kind="completion")


def _error_for(exc: Exception) -> HTTPException:
    if isinstance(exc, CircuitOpenError):
        return HTTPException(status_code=503, detail="Model runtime temporarily unavailable")
    return HTTPException(status_code=502, detail="Upstream model error")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "nexa-model-gateway", "version": app.version}


@app.get("/v1/models")
async def list_models(principal: Principal = Depends(require_principal)):
    data = [
        {
            "id": e.logical,
            "object": "model",
            "runtime": e.runtime,
            "capability": e.capability,
            "version": e.version,
            "aliases": list(e.aliases),
            "circuit": breaker_for(e.runtime).state,
        }
        for e in registry.list_models()
    ]
    return {"object": "list", "data": data}


def _sse(obj) -> str:
    return f"data: {json.dumps(obj)}\n\n"


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
    headers = {"X-NEXA-Trace-Id": trace_id}

    if req.stream:
        return _stream_response(entry, backend, messages, temperature,
                                req.max_tokens, trace_id, principal, headers)

    with Timer() as timer:
        try:
            result = await guarded(
                entry.runtime,
                lambda: backend.chat(entry.backend_model, messages, temperature, req.max_tokens),
            )
        except Exception as exc:
            _record(entry, "error", timer.ms)
            # Log the error type only: upstream messages can echo prompt text.
            log_event(event="chat_error", severity="error", trace_id=trace_id,
                      model=entry.logical, runtime=entry.runtime,
                      actor=principal.subject, error=type(exc).__name__)
            raise _error_for(exc) from exc

    usage = result.get("usage", {})
    prompt_tokens, completion_tokens = _tokens(usage)
    _record(entry, "ok", timer.ms, usage)
    log_event(event="chat", trace_id=trace_id, model=entry.logical,
              model_version=entry.version, runtime=entry.runtime,
              latency_ms=timer.ms, actor=principal.subject,
              prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)

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
        "usage": usage,
        "x_nexa": _nexa_trace(entry, trace_id, timer.ms),
    }
    return JSONResponse(body, headers=headers)


def _stream_response(entry, backend, messages, temperature, max_tokens,
                     trace_id, principal, headers):
    # Fail fast with a proper status before the stream starts.
    if not breaker_for(entry.runtime).allow():
        _record(entry, "error", 0.0)
        raise HTTPException(status_code=503, detail="Model runtime temporarily unavailable")

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def chunk(delta: dict, finish=None, **extra) -> str:
        return _sse({
            "id": completion_id, "object": "chat.completion.chunk",
            "created": created, "model": entry.logical,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            **extra,
        })

    async def events():
        start = time.perf_counter()
        usage: dict = {}
        yield chunk({"role": "assistant"})
        try:
            stream = backend.chat_stream(entry.backend_model, messages, temperature, max_tokens)
            async for item in guarded_stream(entry.runtime, stream):
                if item.get("delta"):
                    yield chunk({"content": item["delta"]})
                if item.get("done"):
                    usage = item.get("usage", {}) or {}
        except Exception as exc:
            ms = round((time.perf_counter() - start) * 1000, 1)
            _record(entry, "error", ms)
            log_event(event="chat_error", severity="error", trace_id=trace_id,
                      model=entry.logical, runtime=entry.runtime,
                      actor=principal.subject, error=type(exc).__name__, stream=True)
            yield _sse({"error": {"status": 502, "message": "Upstream model error",
                                  "trace_id": trace_id}})
            yield "data: [DONE]\n\n"
            return
        ms = round((time.perf_counter() - start) * 1000, 1)
        prompt_tokens, completion_tokens = _tokens(usage)
        _record(entry, "ok", ms, usage)
        log_event(event="chat", trace_id=trace_id, model=entry.logical,
                  model_version=entry.version, runtime=entry.runtime,
                  latency_ms=ms, actor=principal.subject, stream=True,
                  prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        yield chunk({}, "stop", usage=usage, x_nexa=_nexa_trace(entry, trace_id, ms, usage))
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)


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
            vectors = await guarded(
                entry.runtime, lambda: backend.embeddings(entry.backend_model, inputs)
            )
        except Exception as exc:
            _record(entry, "error", timer.ms)
            log_event(event="embed_error", severity="error", trace_id=trace_id,
                      model=entry.logical, runtime=entry.runtime,
                      actor=principal.subject, error=type(exc).__name__)
            raise _error_for(exc) from exc

    _record(entry, "ok", timer.ms)
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
