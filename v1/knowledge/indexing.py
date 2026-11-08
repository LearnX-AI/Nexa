"""Index versioning and the indexing workflow (NEXA-M2-004).

Rules:
- Exactly one index is active; queries go only to it.
- Every write (a newly approved source, a reprocessed run, a supersession) goes
  to the active index and to any index still building, so a build started
  before an upload does not miss it.
- A new index is built from the registry (every active, approved source) beside
  the active one. It must validate before activation: every expected chunk
  present, nothing stale, and self-retrieval recall@5 on a sample at or above
  NEXA_INDEX_SELF_RECALL_MIN.
- Activation is one atomic alias switch. The previous index is kept as ready for
  rollback; rollback first catches it up on anything indexed since the switch,
  and refuses if its model is no longer served.
- A query is embedded with the active index's model and the gateway's reply is
  checked against the index fingerprint; a mismatch refuses the query instead
  of comparing vectors from two different models.
- Re-indexing a version writes the new chunks before removing old ones, so a
  source is never briefly missing from the index.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
from typing import Any, Optional

from . import config
from .embeddings import Embedder, EmbeddingError, ModelChanged, ModelInfo
from .ingestion.registry import Registry
from .store import IndexRecord, StoreError, VectorStore

log = logging.getLogger("nexa.knowledge.indexing")


class IndexError_(Exception):
    """Index lifecycle error with an HTTP-friendly status."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def info_of(rec: IndexRecord) -> ModelInfo:
    return ModelInfo(rec.logical, rec.version, rec.runtime, rec.dim, rec.prefix_scheme)


def store_metadata(c: dict[str, Any]) -> dict[str, Any]:
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


class IndexManager:
    def __init__(self, store: VectorStore, registry: Registry) -> None:
        self.store = store
        self.registry = registry
        self._embedders: dict[str, Embedder] = {}

    def embedder(self, logical: Optional[str] = None) -> Embedder:
        logical = logical or config.EMBED_MODEL
        if logical not in self._embedders:
            self._embedders[logical] = Embedder(logical, cache=self.store)
        return self._embedders[logical]

    # ------------------------------------------------------------ lookup

    async def active(self) -> Optional[IndexRecord]:
        iid = await self.store.active_index_id()
        return await self.store.get_index(iid) if iid else None

    async def _new_record(self, info: ModelInfo, note: str = "") -> IndexRecord:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
        base = f"idx_{stamp}_{info.short()}"
        existing = {r.index_id for r in await self.store.list_indexes()}
        iid, n = base, 1
        while iid in existing:
            n += 1
            iid = f"{base}_{n}"
        rec = IndexRecord(index_id=iid, logical=info.logical, version=info.version, runtime=info.runtime,
                          dim=info.dim, prefix_scheme=info.prefix_scheme, note=note)
        return await self.store.create_index(rec)

    async def ensure_active(self, trace_id: Optional[str] = None) -> IndexRecord:
        """First use: create and activate an empty index for the default model."""
        rec = await self.active()
        if rec:
            return rec
        info = await self.embedder().describe(trace_id)
        rec = await self._new_record(info, note="bootstrap")
        await self.store.update_index(rec.index_id, status="ready", validation={"passed": True, "bootstrap": True})
        return await self.store.set_active(rec.index_id)

    async def _targets(self) -> list[IndexRecord]:
        return [r for r in await self.store.list_indexes() if r.status in ("active", "building")]

    # ------------------------------------------------------------ writes

    def _approved_active(self, version_id: str) -> Optional[dict[str, Any]]:
        src = self.registry.get_source(version_id)
        if src is None or src["status"] != "active" or src["metadata"].get("approval_status") != "approved":
            return None
        return src

    async def _write_version(self, rec: IndexRecord, version_id: str, chunks: list[dict[str, Any]],
                             trace_id: Optional[str]) -> int:
        emb = self.embedder(rec.logical)
        vectors, _ = await emb.embed_documents([c["embedding_text"] for c in chunks], trace_id, expect=info_of(rec))
        items = [{"chunk_id": c["chunk_id"], "version_id": version_id, "text": c["text"], "vector": v,
                  "metadata": store_metadata(c)} for c, v in zip(chunks, vectors)]
        await self.store.upsert(rec.index_id, items)
        # Remove this version's chunks from earlier runs only after the new ones are in.
        keep = {c["chunk_id"] for c in chunks}
        stale = [cid for cid in await self._version_chunk_ids(rec.index_id, version_id) if cid not in keep]
        await self.store.delete_chunks(rec.index_id, stale)
        return len(items)

    async def _version_chunk_ids(self, index_id: str, version_id: str) -> list[str]:
        every = self.registry.chunks_of(version_id, active_only=False)
        present = await self.store.chunk_ids(index_id)
        return [c["chunk_id"] for c in every if c["chunk_id"] in present]

    async def index_version(self, version_id: str, trace_id: Optional[str] = None) -> dict[str, Any]:
        """Embed one approved source version into every write target. Superseded
        versions of the same source are removed. Raises EmbeddingError."""
        src = self._approved_active(version_id)
        if src is None:
            return {"indexed": 0, "skipped": "not an active, approved source"}
        await self.ensure_active(trace_id)
        chunks = self.registry.chunks_of(version_id)
        out: dict[str, Any] = {"indexed": len(chunks), "indexes": []}
        errors: list[EmbeddingError] = []
        # Each target is written independently: an index whose model the gateway
        # no longer serves fails without stopping a build that is in progress.
        for rec in await self._targets():
            try:
                await self._write_version(rec, version_id, chunks, trace_id)
            except EmbeddingError as exc:
                errors.append(exc)
                continue
            for v in self.registry.versions_of(src["source_id"]):
                if v["version_id"] != version_id:
                    await self.store.delete_version(v["version_id"], rec.index_id)
            out["indexes"].append(rec.index_id)
        active = await self.store.active_index_id()
        if active in out["indexes"]:
            self.registry.mark_embedded([c["chunk_id"] for c in chunks])
        if errors:
            raise errors[0]
        return out

    async def remove_version(self, version_id: str) -> int:
        return await self.store.delete_version(version_id)

    def expected(self) -> dict[str, str]:
        """chunk_id -> version_id for every active chunk of every active, approved source."""
        out: dict[str, str] = {}
        for s in self.registry.list_sources("active"):
            if s["metadata"].get("approval_status") != "approved":
                continue
            for c in self.registry.chunks_of(s["version_id"]):
                out[c["chunk_id"]] = s["version_id"]
        return out

    async def reindex_all(self, index_id: Optional[str] = None, trace_id: Optional[str] = None) -> dict[str, int]:
        """(Re-)embed every active, approved source into one index (default: the
        write targets). Cached vectors make an unchanged corpus cost nothing."""
        if index_id is None:
            await self.ensure_active(trace_id)
        targets = [await self.store.get_index(index_id)] if index_id else await self._targets()
        done = failed = 0
        for s in self.registry.list_sources("active"):
            if s["metadata"].get("approval_status") != "approved":
                continue
            chunks = self.registry.chunks_of(s["version_id"])
            try:
                for rec in targets:
                    await self._write_version(rec, s["version_id"], chunks, trace_id)
                done += 1
            except ModelChanged:
                raise
            except EmbeddingError as exc:
                failed += 1
                log.warning("index %s failed for %s: %s", [t.index_id for t in targets], s["version_id"], exc)
        return {"indexed_sources": done, "failed_sources": failed}

    # ------------------------------------------------------------ lifecycle

    async def start_build(self, logical: Optional[str] = None, note: str = "",
                          trace_id: Optional[str] = None) -> IndexRecord:
        try:
            info = await self.embedder(logical).describe(trace_id)
        except EmbeddingError as exc:
            raise IndexError_(502, f"cannot reach embedding model {logical or config.EMBED_MODEL}: {exc}")
        if any(r.status == "building" for r in await self.store.list_indexes()):
            raise IndexError_(409, "another index is already building")
        return await self._new_record(info, note=note)

    async def run_build(self, index_id: str, activate: bool = False, trace_id: Optional[str] = None) -> IndexRecord:
        rec = await self.store.get_index(index_id)
        started = dt.datetime.now(dt.timezone.utc)
        emb = self.embedder(rec.logical)
        calls0, hits0 = emb.calls, emb.cache_hits
        try:
            result = await self.reindex_all(index_id=index_id, trace_id=trace_id)
        except EmbeddingError as exc:
            return await self.store.update_index(index_id, status="failed", note=f"{rec.note} build failed: {exc}"[:500])
        stats = {**result, "gateway_calls": emb.calls - calls0, "cache_hits": emb.cache_hits - hits0,
                 "seconds": round((dt.datetime.now(dt.timezone.utc) - started).total_seconds(), 1)}
        try:
            validation = await self.validate(index_id, trace_id)
        except EmbeddingError as exc:
            validation = {"passed": False, "error": str(exc)[:300]}
        status = "ready" if validation["passed"] and not result["failed_sources"] else "failed"
        rec = await self.store.update_index(index_id, status=status, stats=stats, validation=validation)
        if activate and status == "ready":
            rec = await self.activate(index_id, trace_id=trace_id)
        return rec

    async def validate(self, index_id: str, trace_id: Optional[str] = None) -> dict[str, Any]:
        rec = await self.store.get_index(index_id)
        expected = self.expected()
        stored = {c for c in await self.store.chunk_ids(index_id) if not c.startswith("raw_")}
        missing = sorted(set(expected) - stored)
        extra = sorted(stored - set(expected))
        sample = sorted(set(expected) & stored)
        n = config.INDEX_VALIDATION_SAMPLE
        if len(sample) > n:
            step = len(sample) / n
            sample = [sample[int(i * step)] for i in range(n)]
        hits = 0
        if sample:
            emb = self.embedder(rec.logical)
            texts = self._texts(sample)
            for cid in sample:
                q, _ = await emb.embed_query(texts[cid][:600], trace_id, expect=info_of(rec))
                top = await self.store.query(index_id, q, 5)
                hits += any(r["chunk_id"] == cid for r in top)
        recall = round(hits / len(sample), 3) if sample else None
        passed = not missing and not extra and (recall is None or recall >= config.INDEX_SELF_RECALL_MIN)
        return {"passed": passed, "expected_chunks": len(expected), "stored_chunks": len(stored),
                "missing": len(missing), "missing_sample": missing[:10], "stale": len(extra),
                "stale_sample": extra[:10], "self_recall_at_5": recall, "sampled": len(sample),
                "threshold": config.INDEX_SELF_RECALL_MIN,
                "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}

    def _texts(self, chunk_ids: list[str]) -> dict[str, str]:
        want = set(chunk_ids)
        out: dict[str, str] = {}
        for s in self.registry.list_sources("active"):
            for c in self.registry.chunks_of(s["version_id"]):
                if c["chunk_id"] in want:
                    out[c["chunk_id"]] = c["text"]
        return out

    async def _require_served(self, rec: IndexRecord, action: str, trace_id: Optional[str]) -> None:
        """The gateway must serve exactly the model this index was built with.
        Checked up front: cached vectors would otherwise hide a model change."""
        try:
            now = await self.embedder(rec.logical).describe(trace_id)
        except EmbeddingError as exc:
            raise IndexError_(502, f"cannot {action}: embedding model unreachable: {exc}")
        if now.fingerprint != rec.fingerprint:
            raise IndexError_(409, f"cannot {action}: index {rec.index_id} was built with {rec.fingerprint}, "
                                   f"the gateway now serves {now.fingerprint}")

    async def activate(self, index_id: str, force: bool = False, trace_id: Optional[str] = None) -> IndexRecord:
        rec = await self.store.get_index(index_id)
        if rec is None:
            raise IndexError_(404, "index not found")
        if rec.status == "active":
            return rec
        if rec.status not in ("ready",):
            raise IndexError_(409, f"index is {rec.status}; only a ready index can be activated")
        # Never activate an index the gateway cannot query, even with force.
        await self._require_served(rec, "activate", trace_id)
        validation = await self.validate(index_id, trace_id)
        await self.store.update_index(index_id, validation=validation)
        if not validation["passed"] and not force:
            raise IndexError_(409, f"index failed validation: {validation['missing']} missing, "
                                   f"{validation['stale']} stale, self-recall {validation['self_recall_at_5']}")
        return await self.store.set_active(index_id)

    async def rollback(self, trace_id: Optional[str] = None) -> IndexRecord:
        cur = await self.active()
        if cur is None or not cur.previous_index_id:
            raise IndexError_(409, "no previous index to roll back to")
        prev = await self.store.get_index(cur.previous_index_id)
        if prev is None or prev.status != "ready":
            raise IndexError_(409, f"previous index {cur.previous_index_id} is not available "
                                   f"({prev.status if prev else 'missing'})")
        await self._require_served(prev, "roll back", trace_id)
        # Catch up on everything indexed since the switch, with the old model.
        try:
            await self.reindex_all(index_id=prev.index_id, trace_id=trace_id)
        except ModelChanged as exc:
            raise IndexError_(409, f"cannot roll back: {exc}")
        return await self.activate(prev.index_id, trace_id=trace_id)

    async def retire(self, index_id: str) -> IndexRecord:
        if index_id == await self.store.active_index_id():
            raise IndexError_(409, "cannot retire the active index")
        if await self.store.get_index(index_id) is None:
            raise IndexError_(404, "index not found")
        try:
            await self.store.drop_index(index_id)
        except StoreError as exc:
            raise IndexError_(409, str(exc))
        return await self.store.get_index(index_id)

    # ------------------------------------------------------------ query

    async def query(self, text: str, top_k: int, where=None, trace_id: Optional[str] = None) -> tuple[list, IndexRecord]:
        rec = await self.active()
        if rec is None:
            return [], None
        vec, _ = await self.embedder(rec.logical).embed_query(text, trace_id, expect=info_of(rec))
        return await self.store.query(rec.index_id, vec, top_k, where=where), rec

    async def add_raw(self, docs: list[dict[str, Any]], trace_id: Optional[str] = None) -> int:
        """M1 raw-text ingest into the active index (no source contract)."""
        rec = await self.ensure_active(trace_id)
        vectors, _ = await self.embedder(rec.logical).embed_documents([d["text"] for d in docs], trace_id,
                                                                     expect=info_of(rec))
        items = []
        for d, v in zip(docs, vectors):
            cid = "raw_" + hashlib.sha256((d["text"] + repr(sorted((d.get("metadata") or {}).items()))).encode()
                                          ).hexdigest()[:24]
            items.append({"chunk_id": cid, "version_id": "raw:" + cid, "text": d["text"], "vector": v,
                          "metadata": dict(d.get("metadata") or {})})
        return await self.store.upsert(rec.index_id, items)
