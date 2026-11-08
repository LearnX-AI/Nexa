"""Embedding service for the Knowledge Service (NEXA-M2-004).

All embedding traffic goes through the Model Gateway (NEXA-ADR-0001), never a
direct embedding SDK. On top of the gateway call this module adds:

- Task prefixes. Asymmetric retrieval models expect different prefixes for
  documents and queries (nomic-embed-text: "search_document: " and
  "search_query: "). v0 applied them (main.py PrefixedOllamaEmbeddings, whose
  comment notes retrieval returns the wrong subject without them); the M1
  client did not. The scheme is chosen from the model version the gateway
  reports, or set explicitly.
- Model identity. Every call returns the logical model, the backend version and
  runtime the gateway reports, and the vector dimension. Together with the
  prefix scheme these form the fingerprint stamped on every index version, so
  vectors from different models are never compared.
- Validation. Vectors must be finite, non-zero and of one dimension; they are
  L2-normalised so cosine and inner product agree.
- Batching and a content-addressed cache keyed by (fingerprint, text hash), so
  unchanged text is never re-embedded by a rebuild or reprocessing run.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Optional, Protocol

import httpx

from . import config


class EmbeddingError(Exception):
    pass


# model version prefix -> (document prefix, query prefix)
PREFIX_SCHEMES: dict[str, tuple[str, str]] = {
    "nomic-embed": ("search_document: ", "search_query: "),
    "e5": ("passage: ", "query: "),
    "multilingual-e5": ("passage: ", "query: "),
    "bge": ("", "Represent this sentence for searching relevant passages: "),
    "mxbai-embed": ("", "Represent this sentence for searching relevant passages: "),
}


def prefix_scheme_for(model_version: str) -> tuple[str, str, str]:
    """(scheme name, document prefix, query prefix) for a backend model version."""
    if config.EMBED_PREFIX_SCHEME == "none":
        return "none", "", ""
    if config.EMBED_PREFIX_SCHEME == "custom":
        return "custom", config.EMBED_DOC_PREFIX, config.EMBED_QUERY_PREFIX
    v = (model_version or "").lower().split("/")[-1]
    for key in sorted(PREFIX_SCHEMES, key=len, reverse=True):
        if v.startswith(key):
            return key, *PREFIX_SCHEMES[key]
    return "none", "", ""


@dataclass(frozen=True)
class ModelInfo:
    logical: str
    version: str
    runtime: str
    dim: int
    prefix_scheme: str

    @property
    def fingerprint(self) -> str:
        return f"{self.logical}|{self.version}|{self.runtime}|{self.dim}|{self.prefix_scheme}"

    def short(self) -> str:
        return hashlib.sha256(self.fingerprint.encode()).hexdigest()[:8]


class EmbeddingCache(Protocol):
    async def get_many(self, fingerprint: str, keys: list[str]) -> dict[str, list[float]]: ...

    async def put_many(self, fingerprint: str, items: dict[str, list[float]]) -> None: ...


class MemoryCache:
    def __init__(self, max_items: int = 50_000) -> None:
        self._d: dict[tuple[str, str], list[float]] = {}
        self.max_items = max_items

    async def get_many(self, fingerprint, keys):
        return {k: self._d[(fingerprint, k)] for k in keys if (fingerprint, k) in self._d}

    async def put_many(self, fingerprint, items):
        if len(self._d) + len(items) > self.max_items:
            self._d.clear()
        for k, v in items.items():
            self._d[(fingerprint, k)] = v


def _normalise(vec: list[float], expect_dim: Optional[int]) -> list[float]:
    if not vec or (expect_dim and len(vec) != expect_dim):
        raise EmbeddingError(f"embedding has dimension {len(vec)}, expected {expect_dim}")
    if any(not math.isfinite(x) for x in vec):
        raise EmbeddingError("embedding contains non-finite values")
    n = math.sqrt(sum(x * x for x in vec))
    if n == 0.0:
        raise EmbeddingError("embedding is all zeros")
    return [x / n for x in vec]


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _gateway(texts: list[str], model: str, trace_id: Optional[str]) -> tuple[list[list[float]], dict]:
    """One gateway call. Returns raw vectors and the x_nexa block."""
    if not config.GATEWAY_TOKEN:
        raise EmbeddingError("NEXA_KNOWLEDGE_GATEWAY_TOKEN not set")
    headers = {"Authorization": f"Bearer {config.GATEWAY_TOKEN}"}
    if trace_id:
        headers["X-NEXA-Trace-Id"] = trace_id
    url = f"{config.GATEWAY_URL}/v1/embeddings"
    last: Optional[Exception] = None
    for _ in range(2):  # the gateway already retries its backend; one retry here covers the hop to it
        try:
            async with httpx.AsyncClient(timeout=config.GATEWAY_TIMEOUT) as client:
                resp = await client.post(url, json={"model": model, "input": texts}, headers=headers)
        except httpx.RequestError as exc:
            last = EmbeddingError(f"gateway unreachable: {exc}")
            continue
        if resp.status_code >= 500:
            last = EmbeddingError(f"gateway {resp.status_code}: {resp.text[:200]}")
            continue
        if resp.status_code != 200:
            raise EmbeddingError(f"gateway {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        try:
            rows = sorted(data["data"], key=lambda r: r.get("index", 0))
            return [r["embedding"] for r in rows], data.get("x_nexa") or {}
        except (KeyError, TypeError):
            raise EmbeddingError("unexpected embeddings response shape")
    raise last or EmbeddingError("gateway call failed")


class Embedder:
    """Embeds documents and queries for one logical model."""

    def __init__(self, logical: Optional[str] = None, cache: Optional[EmbeddingCache] = None) -> None:
        self.logical = logical or config.EMBED_MODEL
        self.cache = cache or MemoryCache()
        self._info: Optional[ModelInfo] = None
        self.calls = 0           # gateway calls, for tests and the job report
        self.cache_hits = 0

    def _info_from(self, xn: dict, dim: int) -> ModelInfo:
        version = str(xn.get("model_version") or self.logical)
        scheme, _, _ = prefix_scheme_for(version)
        return ModelInfo(self.logical, version, str(xn.get("runtime") or "unknown"), dim, scheme)

    async def describe(self, trace_id: Optional[str] = None) -> ModelInfo:
        """Probe the gateway for the model's identity and dimension."""
        vecs, xn = await _gateway(["dimension probe"], self.logical, trace_id)
        self.calls += 1
        info = self._info_from(xn, len(vecs[0]) if vecs and vecs[0] else 0)
        if not info.dim:
            raise EmbeddingError("gateway returned an empty embedding")
        self._info = info
        return info

    async def _embed(self, texts: list[str], prefix_kind: str, trace_id: Optional[str],
                     expect: Optional[ModelInfo]) -> tuple[list[list[float]], ModelInfo]:
        info = expect or self._info or await self.describe(trace_id)
        _, doc_p, query_p = prefix_scheme_for(info.version)
        prefix = doc_p if prefix_kind == "document" else query_p
        prepared = [prefix + t for t in texts]
        keys = [text_key(p) for p in prepared]
        cached = await self.cache.get_many(info.fingerprint, keys) if config.EMBED_CACHE else {}
        self.cache_hits += len(cached)
        missing = [i for i, k in enumerate(keys) if k not in cached]
        fresh: dict[str, list[float]] = {}
        for start in range(0, len(missing), config.EMBED_BATCH):
            idx = missing[start:start + config.EMBED_BATCH]
            vecs, xn = await _gateway([prepared[i] for i in idx], self.logical, trace_id)
            self.calls += 1
            if len(vecs) != len(idx):
                raise EmbeddingError("embedding count mismatch")
            got = self._info_from(xn, len(vecs[0]) if vecs and vecs[0] else 0)
            if got.fingerprint != info.fingerprint:
                raise ModelChanged(info, got)
            for i, v in zip(idx, vecs):
                fresh[keys[i]] = _normalise(v, info.dim)
        if fresh and config.EMBED_CACHE:
            await self.cache.put_many(info.fingerprint, fresh)
        merged = {**cached, **fresh}
        return [merged[k] for k in keys], info

    async def embed_documents(self, texts: list[str], trace_id: Optional[str] = None,
                              expect: Optional[ModelInfo] = None) -> tuple[list[list[float]], ModelInfo]:
        return await self._embed(texts, "document", trace_id, expect)

    async def embed_query(self, text: str, trace_id: Optional[str] = None,
                          expect: Optional[ModelInfo] = None) -> tuple[list[float], ModelInfo]:
        # Queries are never cached: they are short, varied and may be personal.
        info = expect or self._info or await self.describe(trace_id)
        _, _, query_p = prefix_scheme_for(info.version)
        vecs, xn = await _gateway([query_p + text], self.logical, trace_id)
        self.calls += 1
        got = self._info_from(xn, len(vecs[0]) if vecs and vecs[0] else 0)
        if got.fingerprint != info.fingerprint:
            raise ModelChanged(info, got)
        return _normalise(vecs[0], info.dim), info


class ModelChanged(EmbeddingError):
    """The gateway now serves a different model than the index was built with.
    Comparing vectors across models gives meaningless scores, so callers must
    refuse rather than answer."""

    def __init__(self, expected: ModelInfo, got: ModelInfo):
        super().__init__(f"embedding model changed: index built with {expected.fingerprint}, "
                         f"gateway now serves {got.fingerprint}; build and activate a new index")
        self.expected = expected
        self.got = got


async def embed(texts: list[str], trace_id: Optional[str] = None) -> list[list[float]]:
    """M1 compatibility: raw document embeddings for the default model, no prefixes."""
    if not texts:
        return []
    vecs, _ = await _gateway(texts, config.EMBED_MODEL, trace_id)
    return vecs
