"""The school server's read-only knowledge store (NEXA-M2-009).

Serves an installed edge package (package.py) behind the same VectorStore
interface the core's pgvector store implements, so the school server runs the
unchanged retrieval, authority, grounding, outline and spelling code: what a
student gets at school is what the core would give for the same approved
content.

Layout under NEXA_EDGE_HOME:

    packages/v<N>/knowledge.sqlite   installed package versions
    packages/v<N>/manifest.json
    current                          version number now served
    previous                         version to roll back to

The store follows `current`: when sync installs a new version the next
request sees it, with no restart. Vectors are held in memory (float16 in the
package, float32 at run time: about 3 KB per chunk) and searched exactly; a
school's package is a few thousand to a few tens of thousands of chunks, well
within a Jetson's memory and well under 50 ms per query.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from typing import Any, Optional

import numpy as np

from ..knowledge.store import IndexRecord, StoreError, VectorStore
from .package import vec_from

_FTS_WORD = re.compile(r"[A-Za-z0-9]{2,}")


def current_version(home: str) -> Optional[int]:
    try:
        with open(os.path.join(home, "current")) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def package_dir(home: str, version: int) -> str:
    return os.path.join(home, "packages", f"v{version}")


class _Loaded:
    def __init__(self, home: str, version: int):
        d = package_dir(home, version)
        with open(os.path.join(d, "manifest.json")) as fh:
            self.manifest = json.load(fh)
        self.version = version
        self.path = os.path.join(d, "knowledge.sqlite")
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        rows = con.execute("SELECT chunk_id, version_id, text, metadata, vector FROM chunks ORDER BY chunk_id").fetchall()
        self.con = con
        self.ids = [r[0] for r in rows]
        self.pos = {c: i for i, c in enumerate(self.ids)}
        self.items = [{"chunk_id": r[0], "version_id": r[1], "text": r[2], "metadata": json.loads(r[3])}
                      for r in rows]
        dim = int(self.manifest["index"]["dim"])
        mat = np.frombuffer(b"".join(r[4] for r in rows), dtype="<f2").astype(np.float32) if rows \
            else np.zeros((0,), np.float32)
        mat = mat.reshape(len(rows), dim) if rows else np.zeros((0, dim), np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        self.mat = mat / np.where(norms == 0, 1, norms)
        idx = self.manifest["index"]
        self.record = IndexRecord(
            index_id=f"edge_{self.manifest['school_id']}_v{version}", logical=idx["logical"], version=idx["version"],
            runtime=idx["runtime"], dim=dim, prefix_scheme=idx["prefix_scheme"], status="active",
            note=f"school package v{version} built {self.manifest['created_at']} from core index {idx.get('index_id')}",
            stats={"chunks": len(rows), "sources": self.manifest["stats"]["sources"]})


class EdgeStore(VectorStore):
    """Read-only. Writes raise: content changes happen on core NEXA and arrive
    in the next package."""

    name = "edge"

    def __init__(self, home: str) -> None:
        self.home = home
        self._loaded: Optional[_Loaded] = None
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], list[float]] = {}

    # ------------------------------------------------------------ package
    def _pkg(self) -> Optional[_Loaded]:
        v = current_version(self.home)
        if v is None:
            return None
        if self._loaded is None or self._loaded.version != v:
            with self._lock:
                if self._loaded is None or self._loaded.version != v:
                    old, self._loaded = self._loaded, _Loaded(self.home, v)
                    if old is not None:
                        old.con.close()
        return self._loaded

    def manifest(self) -> Optional[dict[str, Any]]:
        p = self._pkg()
        return p.manifest if p else None

    # ------------------------------------------------------------ index records
    async def active_index_id(self):
        p = self._pkg()
        return p.record.index_id if p else None

    async def get_index(self, index_id):
        p = self._pkg()
        return p.record if p and p.record.index_id == index_id else None

    async def list_indexes(self):
        p = self._pkg()
        return [p.record] if p else []

    async def create_index(self, rec):
        raise StoreError("read-only school node: indexes are built on core NEXA")

    async def update_index(self, index_id, **fields):
        raise StoreError("read-only school node")

    async def set_active(self, index_id):
        raise StoreError("read-only school node: install a package to change the active index")

    async def drop_index(self, index_id):
        raise StoreError("read-only school node")

    async def upsert(self, index_id, items):
        raise StoreError("read-only school node: content changes arrive in packages from core NEXA")

    async def delete_version(self, version_id, index_id=None):
        raise StoreError("read-only school node")

    async def delete_chunks(self, index_id, chunk_ids):
        raise StoreError("read-only school node")

    # ------------------------------------------------------------ reads
    def _p(self, index_id: str) -> _Loaded:
        p = self._pkg()
        if p is None or p.record.index_id != index_id:
            raise StoreError(f"unknown index {index_id}")
        return p

    async def query(self, index_id, vector, top_k, where=None):
        p = self._p(index_id)
        if not p.ids:
            return []
        q = np.asarray(vector, dtype=np.float32)
        n = float(np.linalg.norm(q)) or 1.0
        scores = p.mat @ (q / n)
        out = []
        for i in np.argsort(-scores, kind="stable"):
            it = p.items[int(i)]
            if where is not None and not where(it["metadata"]):
                continue
            out.append({"chunk_id": it["chunk_id"], "text": it["text"], "metadata": dict(it["metadata"]),
                        "score": float(scores[int(i)])})
            if len(out) >= top_k:
                break
        return out

    async def lexical_query(self, index_id, query, top_k, where=None):
        """FTS5 BM25 with Porter stemming, any query term matching (as the
        core's full-text ranking also rewards partial matches)."""
        p = self._p(index_id)
        terms = list(dict.fromkeys(w.lower() for w in _FTS_WORD.findall(query or "")))
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        rows = p.con.execute("SELECT chunk_id, bm25(chunks_fts) FROM chunks_fts WHERE chunks_fts MATCH ? "
                             "ORDER BY bm25(chunks_fts) LIMIT ?", (match, max(top_k * 5, 200))).fetchall()
        out = []
        for cid, score in rows:
            i = p.pos.get(cid)
            if i is None:
                continue
            it = p.items[i]
            if where is not None and not where(it["metadata"]):
                continue
            out.append({"chunk_id": cid, "text": it["text"], "metadata": dict(it["metadata"]), "score": -float(score)})
            if len(out) >= top_k:
                break
        return out

    def lexical_available(self, index_id):
        return True

    async def chunk_ids(self, index_id):
        return set(self._p(index_id).ids)

    async def get_vectors(self, index_id, chunk_ids):
        p = self._p(index_id)
        return {c: p.mat[p.pos[c]].tolist() for c in chunk_ids if c in p.pos}

    async def get_items(self, index_id, chunk_ids):
        p = self._p(index_id)
        out = {}
        for c in chunk_ids:
            i = p.pos.get(c)
            if i is not None:
                it = p.items[i]
                out[c] = {**it, "metadata": dict(it["metadata"]), "vector": p.mat[i].tolist()}
        return out

    async def all_texts(self, index_id):
        return [it["text"] for it in self._p(index_id).items]

    async def list_by_source_type(self, index_id, source_type):
        return [{"chunk_id": it["chunk_id"], "text": it["text"], "metadata": dict(it["metadata"])}
                for it in self._p(index_id).items if it["metadata"].get("source_type") == source_type]

    # query-embedding cache (in memory; questions repeat in a classroom)
    async def get_many(self, fingerprint, keys):
        return {k: self._cache[(fingerprint, k)] for k in keys if (fingerprint, k) in self._cache}

    async def put_many(self, fingerprint, items):
        if len(self._cache) > 20000:
            self._cache.clear()
        for k, v in items.items():
            self._cache[(fingerprint, k)] = v


__all__ = ["EdgeStore", "current_version", "package_dir", "vec_from"]
