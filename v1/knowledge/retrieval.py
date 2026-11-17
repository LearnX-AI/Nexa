"""Hybrid retrieval, filtering and reranking (NEXA-M2-005).

One request runs these stages, each timed and explained:

1. Filters. The caller's access (approval, access class, effective dates,
   school scope) plus the request's narrowing filters (subject, grade,
   language, curriculum codes, source type, authority tier, scope) form one
   RetrievalFilter. Both retrievers apply it inside the query, so a passage
   the caller may not see is never a candidate, not even for fusion.
2. Candidates. The vector retriever (HNSW cosine) and the lexical retriever
   (PostgreSQL full-text search, or BM25 in the in-memory store) each return
   up to `candidates` passages.
3. Fusion. Reciprocal rank fusion: score = sum over retrievers of
   weight / (k + rank). It needs no score calibration between retrievers, which
   matters because cosine and ts_rank are on unrelated scales.
4. Rerank (optional). The top `rerank_pool` fused passages are re-scored by a
   reranker. The shipped reranker is feature-based (semantic similarity, query
   term coverage, phrase matches, heading matches): cheap, deterministic and
   edge-friendly. Whether it is on by default is decided by the benchmark.
5. Authority. authority.arbitrate() applies the M2-001 rules to the top
   passages: an official passage covering the same curriculum point as a
   lower-authority one is placed above it, never silently replaced.
6. Diversity. diversify() drops near copies of a higher-ranked passage (the
   same lesson synced from several Moodle courses; by shingle Jaccard, or by
   containment when one copy is split differently), keeps reference lists
   (bibliographies) out unless the question asks for sources, and, if
   NEXA_MAX_PER_SOURCE is set, caps passages per source (off by default);
   dropped passages fill in only when too few remain.
7. Cut to top_k. Every result keeps its stage scores and the reasons it was
   selected, and the response carries the stage timings and what was applied.

`score` on a result is its semantic similarity to the query (cosine), the same
meaning it had in M1, so it can be compared across queries and used to judge
evidence strength (NEXA-M2-006). Results are returned in final rank order,
which is not always score order.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import config
from .access import RetrievalFilter
from .authority import arbitrate, classify_claim
from .benchmark.bm25 import tokenize
from .indexing import IndexManager, info_of
from .store import lexical_text, stem

MODES = ("hybrid", "vector", "lexical")


@dataclass
class RetrievalRequest:
    query: str
    top_k: int = 5
    filters: dict[str, Any] = field(default_factory=dict)
    mode: Optional[str] = None          # default config.RETRIEVAL_MODE
    rerank: Optional[bool] = None       # default config.RERANK
    profile: Optional[str] = None       # default config.RETRIEVAL_PROFILE
    context: Optional[dict[str, Any]] = None   # {"grades": [...], "subjects": [...]}: a preference


@dataclass
class RetrievalResult:
    results: list[dict[str, Any]]
    explain: dict[str, Any]
    index: Any = None
    query_vector: Optional[list[float]] = None


def _terms(text: str) -> list[str]:
    return [stem(t) for t in tokenize(text)]


def _cos(a: Optional[list[float]], b: Optional[list[float]]) -> Optional[float]:
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else None


def query_coverage(query: str, item: dict[str, Any]) -> dict[str, float]:
    """How much of the question the passage literally covers: share of the
    query's content words present, share of its adjacent word pairs present,
    share present in the title or section path. Language-independent, so it
    also helps judge evidence strength across embedding models."""
    q = list(dict.fromkeys(_terms(query)))
    if not q:
        return {"coverage": 0.0, "phrase": 0.0, "heading": 0.0}
    md = item.get("metadata") or {}
    body = _terms(lexical_text(item))
    bset = set(body)
    head = set(_terms(f"{md.get('title') or ''} {md.get('section_path') or ''}"))
    pairs = list(zip(q, q[1:]))
    bpairs = set(zip(body, body[1:]))
    return {
        "coverage": round(sum(t in bset for t in q) / len(q), 4),
        "phrase": round(sum(p in bpairs for p in pairs) / len(pairs), 4) if pairs else 0.0,
        "heading": round(sum(t in head for t in q) / len(q), 4),
    }


class FeatureReranker:
    """Re-scores fused candidates from features of the query and passage.

    rerank = w_sim * cosine + w_cov * coverage + w_phrase * phrase
             + w_head * heading + w_rrf * (fused score / best fused score)

    The fused-score term keeps the retrievers' agreement in play, so the
    reranker refines the order rather than replacing it."""

    name = "features-1.0"
    weights = {"similarity": 0.40, "coverage": 0.25, "phrase": 0.10, "heading": 0.05, "fused": 0.20}

    def score(self, query: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not items:
            return items
        best = max(i["scores"]["rrf"] for i in items) or 1.0
        w = self.weights
        for it in items:
            cov = it.get("_coverage") or query_coverage(query, it)
            it["_coverage"] = cov
            sim = it["scores"].get("similarity") or 0.0
            s = (w["similarity"] * sim + w["coverage"] * cov["coverage"] + w["phrase"] * cov["phrase"]
                 + w["heading"] * cov["heading"] + w["fused"] * it["scores"]["rrf"] / best)
            it["scores"]["rerank"] = round(s, 6)
        return sorted(items, key=lambda r: (-r["scores"]["rerank"], r["chunk_id"]))


def _shingles(text: str, n: int = 5) -> set[str]:
    w = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {" ".join(w[i:i + n]) for i in range(max(1, len(w) - n + 1))}


def _source_key(it: dict[str, Any]) -> str:
    md = it.get("metadata") or {}
    return str(md.get("version_id") or md.get("source_id") or md.get("title") or it.get("chunk_id"))


_REF_MARK = re.compile(r"retrieved\s+(?:from|on)|\(n\.\s?d\.\)|https?://|www\.|doi\.org|"
                       r"\(\d{4}(?:,\s*[a-z]+\s+\d{1,2})?\)", re.I)
_ASKS_FOR_SOURCES = re.compile(r"\b(reference|references|bibliograph|citation|sources?|further reading|"
                               r"website|url|link)\b", re.I)


def is_reference_list(text: str) -> bool:
    """A passage that is mostly a bibliography: at least four reference marks
    and at least one per 25 words."""
    words = len((text or "").split())
    marks = len(_REF_MARK.findall(text or ""))
    return marks >= 4 and words > 0 and marks * 25 >= words


def _near_copy(sh: set[str], seen: list[set[str]]) -> bool:
    for o in seen:
        if not o:
            continue
        inter = len(sh & o)
        if inter / len(sh | o) >= config.DEDUP_SIMILARITY:
            return True
        small = min(len(sh), len(o))
        if small >= 20 and inter / small >= config.DEDUP_CONTAINMENT:
            return True
    return False


def _context(raw: Optional[dict[str, Any]]) -> dict[str, set]:
    raw = raw or {}
    out: dict[str, set] = {}
    if raw.get("grades"):
        out["grades"] = {int(g) for g in raw["grades"]}
    if raw.get("subjects"):
        out["subjects"] = {str(x).strip().lower() for x in raw["subjects"]}
    return out


def _context_bonus(md: dict[str, Any], ctx: dict[str, set]) -> float:
    """CONTEXT_BONUS for a matching grade, and again for a matching subject.
    Passages with no grade (or subject) recorded get nothing and lose nothing."""
    bonus = 0.0
    if "grades" in ctx:
        raw = md.get("grade_levels")
        parts = raw if isinstance(raw, list) else str(raw or "").split(",")
        grades = {int(p) for p in (str(x).strip() for x in parts) if p.isdigit()}
        if grades & ctx["grades"]:
            bonus += config.CONTEXT_BONUS
    if "subjects" in ctx and str(md.get("subject") or "").strip().lower() in ctx["subjects"]:
        bonus += config.CONTEXT_BONUS
    return bonus


def diversify(items: list[dict[str, Any]], top_k: int, query: str = ""
              ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep rank order, but at most MAX_PER_SOURCE passages per source, no
    near copy of a passage already kept (by Jaccard or containment), and no
    reference list unless the question asks for sources. Dropped passages
    fill in only when too few remain."""
    kept: list[dict[str, Any]] = []
    spare: list[dict[str, Any]] = []
    per: dict[str, int] = {}
    seen: list[set[str]] = []
    dropped = {"same_source": 0, "near_duplicate": 0, "reference_list": 0}
    refs_ok = bool(_ASKS_FOR_SOURCES.search(query or ""))
    for it in items:
        if len(kept) >= top_k:
            break
        key = _source_key(it)
        sh = _shingles(it.get("text") or "")
        if config.MAX_PER_SOURCE > 0 and per.get(key, 0) >= config.MAX_PER_SOURCE:
            dropped["same_source"] += 1
            spare.append(it)
            continue
        if sh and _near_copy(sh, seen):
            dropped["near_duplicate"] += 1
            spare.append(it)
            continue
        if config.DEMOTE_REFERENCE_LISTS and not refs_ok and is_reference_list(it.get("text") or ""):
            dropped["reference_list"] += 1
            spare.append(it)
            continue
        kept.append(it)
        per[key] = per.get(key, 0) + 1
        seen.append(sh)
    kept += spare[:max(0, top_k - len(kept))]
    return kept, {k: v for k, v in dropped.items() if v}


class Retriever:
    def __init__(self, index: IndexManager, reranker: Optional[FeatureReranker] = None) -> None:
        self.index = index
        self.reranker = reranker or FeatureReranker()

    async def retrieve(self, req: RetrievalRequest, where: RetrievalFilter,
                       trace_id: Optional[str] = None) -> RetrievalResult:
        t0 = time.perf_counter()
        timings: dict[str, float] = {}

        def lap(name: str, since: float) -> float:
            now = time.perf_counter()
            timings[name] = round((now - since) * 1000, 1)
            return now

        mode = (req.mode or config.RETRIEVAL_MODE).lower()
        if mode not in MODES + ("auto",):
            raise ValueError(f"mode must be one of {MODES}")
        profile_name = req.profile or config.RETRIEVAL_PROFILE
        profile = config.PROFILES.get(profile_name) or config.PROFILES["central"]
        rerank = config.RERANK if req.rerank is None else bool(req.rerank)
        rec = await self.index.active()
        model_default = config.RETRIEVAL_DEFAULTS.get(rec.prefix_scheme if rec else "") \
            or config.RETRIEVAL_DEFAULTS["default"]
        if mode == "auto":
            mode = model_default["mode"]
        if rerank is None:
            rerank = model_default["rerank"] and mode != "vector"
        top_k = max(1, min(int(req.top_k), 50))
        flt = where.narrow(**req.filters) if req.filters else where
        n = max(profile["candidates"], top_k * 4)
        claim = classify_claim(req.query)
        explain: dict[str, Any] = {"mode": mode, "profile": profile_name, "filters": flt.describe(),
                                   "claim_class": claim.value, "reranker": self.reranker.name if rerank else None}

        if rec is None:
            explain.update(index=None, timings_ms={"total": round((time.perf_counter() - t0) * 1000, 1)})
            return RetrievalResult([], explain)
        explain.update(index=rec.index_id, embedding=rec.fingerprint)
        store = self.index.store
        lexical_ok = store.lexical_available(rec.index_id)
        if mode != "vector" and not lexical_ok:
            explain["degraded"] = "lexical retrieval unavailable for this index; vector only"
            mode = "vector"
            explain["mode"] = mode

        t = time.perf_counter()
        qvec: Optional[list[float]] = None
        if mode != "lexical":
            qvec, _ = await self.index.embedder(rec.logical).embed_query(req.query, trace_id, expect=info_of(rec))
            t = lap("embed", t)
        vec_rows = await store.query(rec.index_id, qvec, n, where=flt) if qvec is not None else []
        t = lap("vector", t)
        lex_rows = await store.lexical_query(rec.index_id, req.query, n, where=flt) if mode != "vector" else []
        t = lap("lexical", t)

        # fusion
        fused: dict[str, dict[str, Any]] = {}
        for name, rows, weight in (("vector", vec_rows, config.RRF_WEIGHT_VECTOR),
                                   ("lexical", lex_rows, config.RRF_WEIGHT_LEXICAL)):
            for rank, r in enumerate(rows, 1):
                it = fused.setdefault(r["chunk_id"], {"chunk_id": r["chunk_id"], "text": r["text"],
                                                      "metadata": r.get("metadata") or {},
                                                      "scores": {"rrf": 0.0}, "reasons": []})
                it["scores"][f"{name}_rank"] = rank
                it["scores"][name] = round(float(r["score"]), 6)
                it["scores"]["rrf"] += weight / (config.RRF_K + rank)
        ctx = _context(req.context)
        if ctx:
            for it in fused.values():
                bonus = _context_bonus(it.get("metadata") or {}, ctx)
                if bonus:
                    it["scores"]["context"] = round(bonus, 6)
                    it["scores"]["rrf"] += bonus
                    it["reasons"].append("matches the learner's course")
            explain["context"] = {k: sorted(v) for k, v in ctx.items()}
        ordered = sorted(fused.values(), key=lambda r: (-r["scores"]["rrf"], r["chunk_id"]))
        for i, it in enumerate(ordered, 1):
            it["scores"]["rrf"] = round(it["scores"]["rrf"], 6)
            it["scores"]["fused_rank"] = i
        t = lap("fuse", t)

        # Similarity for every passage that may be returned: lexical-only finds
        # have no cosine yet. One fetch of their vectors.
        pool_n = profile["rerank_pool"] if rerank else max(2 * top_k, 10)
        pool = ordered[:max(pool_n, top_k)]
        if qvec is not None:
            need = [it["chunk_id"] for it in pool if "vector" not in it["scores"]]
            items = await store.get_items(rec.index_id, need) if need else {}
            for it in pool:
                if "vector" in it["scores"]:
                    it["scores"]["similarity"] = it["scores"]["vector"]
                else:
                    it["scores"]["similarity"] = round(_cos(qvec, (items.get(it["chunk_id"]) or {}).get("vector"))
                                                       or 0.0, 6)
        t = lap("similarity", t)

        if rerank:
            pool = self.reranker.score(req.query, pool)
            for i, it in enumerate(pool, 1):
                it["scores"]["rerank_rank"] = i
            t = lap("rerank", t)

        arbitration_pool = pool[:max(3 * top_k, 15)]
        arbitrated, conflicts = arbitrate(arbitration_pool, claim)
        t = lap("authority", t)
        final, dropped = diversify(arbitrated, top_k, req.query)
        if dropped:
            explain["diversity"] = dropped

        for i, it in enumerate(final, 1):
            s = it["scores"]
            found = [f"{k} rank {s[k + '_rank']}" for k in ("vector", "lexical") if k + "_rank" in s]
            it["reasons"].append("found by " + " and ".join(found))
            if rerank and s.get("rerank_rank") != s.get("fused_rank"):
                it["reasons"].append(f"reranked from {s['fused_rank']} to {s['rerank_rank']}")
            pre = s.get("rerank_rank") if rerank else s.get("fused_rank")
            if pre != i:
                it["reasons"].append(f"authority rules moved it from {pre} to {i}")
            it["reasons"].extend(it["authority"]["notes"])
            it["rank"] = i
            it["score"] = s.get("similarity", s.get("lexical", 0.0))
            it["coverage"] = it.pop("_coverage", None) or query_coverage(req.query, it)
        for it in ordered:
            it.pop("_coverage", None)

        explain["candidates"] = {"vector": len(vec_rows), "lexical": len(lex_rows), "fused": len(ordered),
                                 "pool": len(pool)}
        explain["conflicts"] = conflicts
        timings["total"] = round((time.perf_counter() - t0) * 1000, 1)
        explain["timings_ms"] = timings
        return RetrievalResult(final, explain, rec, qvec)
