"""Vector store interface with index versions (NEXA-M2-004), and the in-memory backend.

An index version is one complete set of vectors built by one embedding model
(identified by its fingerprint: logical model, backend version, runtime,
dimension and prefix scheme). Queries go to the single active index. A new
index is built beside it, validated, then activated in one step; the previous
index stays available for rollback until it is retired.

Lifecycle: building -> ready -> active -> ready (after a newer one activates)
-> retired. A failed build ends in failed.

Backends: InMemoryStore (tests, quick DEV) here, PgVectorStore (ADR-0002) in
pgstore.py. Both implement the same contract and are run against the same
tests.
"""
from __future__ import annotations

import copy
import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from . import config

Filter = Callable[[dict[str, Any]], bool]

STATUSES = ("building", "ready", "active", "retired", "failed")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class IndexRecord:
    index_id: str
    logical: str
    version: str
    runtime: str
    dim: int
    prefix_scheme: str
    tokenizer: str = "unknown"
    max_input_tokens: Optional[int] = None
    normalization: str = "l2"
    distance: str = "cosine"
    status: str = "building"
    created_at: str = field(default_factory=_now)
    activated_at: Optional[str] = None
    previous_index_id: Optional[str] = None
    stats: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def fingerprint(self) -> str:
        return f"{self.logical}|{self.version}|{self.runtime}|{self.dim}|{self.prefix_scheme}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fingerprint"] = self.fingerprint
        return d


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class StoreError(Exception):
    pass


def lexical_text(item: dict[str, Any]) -> str:
    md = item.get("metadata") or {}
    return f"{md.get('title') or ''} {md.get('section_path') or ''}\n{item.get('text') or ''}"


_SUFFIXES = ("ational", "ations", "ation", "ities", "ness", "ments", "ment", "ings", "ing", "ies", "ied",
             "ses", "es", "ed", "ly", "s")


def stem(word: str) -> str:
    """A deliberately small suffix stripper, so plural and inflected forms meet
    (cells / cell, replicates / replication). Used by the in-memory store only."""
    for suf in _SUFFIXES:
        if len(word) > len(suf) + 3 and word.endswith(suf):
            return word[: -len(suf)]
    return word


def stem_text(text: str) -> str:
    import re
    return " ".join(stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()))


class VectorStore:
    """Store contract. Items are {chunk_id, version_id, text, vector, metadata}."""

    name = "abstract"

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    # index versions
    async def create_index(self, rec: IndexRecord) -> IndexRecord: raise NotImplementedError
    async def list_indexes(self) -> list[IndexRecord]: raise NotImplementedError
    async def get_index(self, index_id: str) -> Optional[IndexRecord]: raise NotImplementedError
    async def update_index(self, index_id: str, **fields: Any) -> IndexRecord: raise NotImplementedError
    async def active_index_id(self) -> Optional[str]: raise NotImplementedError
    async def set_active(self, index_id: str) -> IndexRecord: raise NotImplementedError
    async def drop_index(self, index_id: str) -> None: raise NotImplementedError

    # vectors
    async def upsert(self, index_id: str, items: list[dict[str, Any]]) -> int: raise NotImplementedError
    async def delete_version(self, version_id: str, index_id: Optional[str] = None) -> int: raise NotImplementedError
    async def delete_chunks(self, index_id: str, chunk_ids: list[str]) -> int: raise NotImplementedError
    async def query(self, index_id: str, vector: list[float], top_k: int,
                    where: Optional[Filter] = None) -> list[dict[str, Any]]: raise NotImplementedError
    async def chunk_ids(self, index_id: str) -> set[str]: raise NotImplementedError
    async def get_vectors(self, index_id: str, chunk_ids: list[str]) -> dict[str, list[float]]:
        raise NotImplementedError

    # M2-005 lexical retrieval and item lookup
    async def lexical_query(self, index_id: str, query: str, top_k: int,
                            where: Optional[Filter] = None) -> list[dict[str, Any]]: raise NotImplementedError
    async def get_items(self, index_id: str, chunk_ids: list[str]) -> dict[str, dict[str, Any]]:
        raise NotImplementedError

    async def all_texts(self, index_id: str) -> list[str]:
        """The text of every chunk (the spelling vocabulary, spelling.py)."""
        raise NotImplementedError

    async def list_by_source_type(self, index_id: str, source_type: str) -> list[dict[str, Any]]:
        """chunk_id, text and metadata of every chunk of one source type
        (curriculum outlines, outline.py)."""
        raise NotImplementedError

    def lexical_available(self, index_id: str) -> bool:
        return True

    # embedding cache (embeddings.EmbeddingCache protocol)
    async def get_many(self, fingerprint: str, keys: list[str]) -> dict[str, list[float]]: raise NotImplementedError
    async def put_many(self, fingerprint: str, items: dict[str, list[float]]) -> None: raise NotImplementedError


class InMemoryStore(VectorStore):
    """Non-persistent, single-process. Lost on restart; see REINDEX_ON_START."""

    name = "memory"

    def __init__(self) -> None:
        self._indexes: dict[str, IndexRecord] = {}
        self._items: dict[str, dict[str, dict[str, Any]]] = {}
        self._active: Optional[str] = None
        self._cache: dict[tuple[str, str], list[float]] = {}
        self._lex: dict[str, tuple[int, Any, list[str]]] = {}   # index -> (generation, BM25, chunk ids)
        self._gen: dict[str, int] = {}

    # Records are copied in and out, as a database would, so a caller's
    # snapshot never changes underneath it.

    async def create_index(self, rec):
        if rec.index_id in self._indexes:
            raise StoreError(f"index {rec.index_id} exists")
        self._indexes[rec.index_id] = copy.deepcopy(rec)
        self._items[rec.index_id] = {}
        return copy.deepcopy(rec)

    async def list_indexes(self):
        return [copy.deepcopy(r) for r in sorted(self._indexes.values(), key=lambda r: (r.created_at, r.index_id))]

    async def get_index(self, index_id):
        rec = self._indexes.get(index_id)
        return copy.deepcopy(rec) if rec else None

    async def update_index(self, index_id, **fields):
        if index_id not in self._indexes:
            raise StoreError(f"unknown index {index_id}")
        rec = self._indexes[index_id]
        for k, v in fields.items():
            setattr(rec, k, v)
        return copy.deepcopy(rec)

    async def active_index_id(self):
        return self._active

    async def set_active(self, index_id):
        rec = self._indexes[index_id]
        prev = self._active
        if prev and prev != index_id and prev in self._indexes:
            self._indexes[prev].status = "ready"
        rec.status, rec.activated_at = "active", _now()
        if prev != index_id:
            rec.previous_index_id = prev
        self._active = index_id
        return copy.deepcopy(rec)

    async def drop_index(self, index_id):
        if index_id == self._active:
            raise StoreError("cannot drop the active index")
        self._items.pop(index_id, None)
        if index_id in self._indexes:
            self._indexes[index_id].status = "retired"

    async def upsert(self, index_id, items):
        bucket = self._items[index_id]
        dim = self._indexes[index_id].dim
        for it in items:
            if len(it["vector"]) != dim:
                raise StoreError(f"vector dimension {len(it['vector'])} does not match index dimension {dim}")
            bucket[it["chunk_id"]] = it
        self._gen[index_id] = self._gen.get(index_id, 0) + 1
        return len(items)

    async def delete_version(self, version_id, index_id=None):
        n = 0
        for iid, bucket in self._items.items():
            if index_id and iid != index_id:
                continue
            gone = [cid for cid, it in bucket.items() if it.get("version_id") == version_id]
            for cid in gone:
                del bucket[cid]
            n += len(gone)
            self._gen[iid] = self._gen.get(iid, 0) + 1
        return n

    async def delete_chunks(self, index_id, chunk_ids):
        bucket = self._items.get(index_id, {})
        n = 0
        for c in chunk_ids:
            n += bucket.pop(c, None) is not None
        self._gen[index_id] = self._gen.get(index_id, 0) + 1
        return n

    async def query(self, index_id, vector, top_k, where=None):
        bucket = self._items.get(index_id, {})
        scored = [
            {"chunk_id": it["chunk_id"], "text": it["text"], "metadata": it.get("metadata", {}),
             "score": _cosine(vector, it["vector"])}
            for it in bucket.values()
            if where is None or where(it.get("metadata") or {})
        ]
        scored.sort(key=lambda r: (-r["score"], r["chunk_id"]))
        return scored[:top_k]

    async def chunk_ids(self, index_id):
        return set(self._items.get(index_id, {}))

    async def get_vectors(self, index_id, chunk_ids):
        bucket = self._items.get(index_id, {})
        return {c: bucket[c]["vector"] for c in chunk_ids if c in bucket}

    async def lexical_query(self, index_id, query, top_k, where=None):
        """BM25 over title, section path and text, with light suffix stemming.
        The pgvector store uses PostgreSQL full-text search instead; the two
        rank similarly but not identically (tests compare membership)."""
        from .benchmark.bm25 import BM25
        bucket = self._items.get(index_id, {})
        gen = self._gen.get(index_id, 0)
        cached = self._lex.get(index_id)
        if cached is None or cached[0] != gen:
            ids = sorted(bucket)
            docs = [lexical_text(bucket[c]) for c in ids]
            cached = (gen, BM25([stem_text(d) for d in docs]), ids)
            self._lex[index_id] = cached
        _, bm, ids = cached
        out = []
        for i, score in bm.search(stem_text(query), k=len(ids)):
            it = bucket[ids[i]]
            if where is not None and not where(it.get("metadata") or {}):
                continue
            out.append({"chunk_id": it["chunk_id"], "text": it["text"], "metadata": it.get("metadata", {}),
                        "score": score})
            if len(out) >= top_k:
                break
        return out

    async def all_texts(self, index_id):
        return [it.get("text") or "" for it in self._items.get(index_id, {}).values()]

    async def list_by_source_type(self, index_id, source_type):
        return [{"chunk_id": c, "text": it.get("text"), "metadata": copy.deepcopy(it.get("metadata") or {})}
                for c, it in self._items.get(index_id, {}).items()
                if (it.get("metadata") or {}).get("source_type") == source_type]

    async def get_items(self, index_id, chunk_ids):
        bucket = self._items.get(index_id, {})
        return {c: copy.deepcopy(bucket[c]) for c in chunk_ids if c in bucket}

    async def get_many(self, fingerprint, keys):
        return {k: self._cache[(fingerprint, k)] for k in keys if (fingerprint, k) in self._cache}

    async def put_many(self, fingerprint, items):
        for k, v in items.items():
            self._cache[(fingerprint, k)] = v


def get_store() -> VectorStore:
    backend = config.STORE_BACKEND
    if backend == "memory":
        return InMemoryStore()
    if backend == "pgvector":
        from .pgstore import PgVectorStore
        return PgVectorStore(config.PG_DSN, schema=config.PG_SCHEMA)
    raise ValueError(f"Unknown store backend: {backend}")
