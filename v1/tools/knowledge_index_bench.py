"""NEXA-M2-004 index benchmark over the real corpus.

    # Offline (no gateway; deterministic 768-d hashing embedder). Measures the
    # store and the index lifecycle, not semantic quality.
    python v1/tools/knowledge_index_bench.py --corpus DIR --pg-dsn DSN --offline

    # On the DGX, through the Model Gateway with the real embedding model.
    # Adds semantic numbers: gold-set recall with and without task prefixes.
    NEXA_KNOWLEDGE_GATEWAY_TOKEN=... python v1/tools/knowledge_index_bench.py --corpus DIR --pg-dsn DSN

DIR is the output of knowledge_corpus.py (its registry). Each run works in a
fresh schema that is dropped at the end. Reports:

- build: chunks, gateway calls, seconds
- rebuild of the unchanged corpus: gateway calls (expected 0, from the cache)
- validation: missing, stale, self-recall@5
- ANN quality: HNSW top-10 overlap with exact (sequential scan) top-10
- latency: p50 / p95 of filtered student queries
- gold set (gateway mode): recall@1/5 and MRR with the configured prefix
  scheme and, with --compare-prefixes, with prefixes disabled
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import uuid

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from v1.identity.principal import Principal  # noqa: E402
from v1.knowledge import config, embeddings  # noqa: E402
from v1.knowledge.access import retrieval_filter  # noqa: E402
from v1.knowledge.indexing import IndexManager  # noqa: E402
from v1.knowledge.ingestion.registry import Registry  # noqa: E402
from v1.knowledge.pgstore import PgVectorStore  # noqa: E402

GOLD = os.path.join(ROOT, "v1", "knowledge", "benchmark", "gold_questions.json")
_W = re.compile(r"[a-z0-9]+")


def offline_gateway(dim: int = 768):
    """Deterministic hashing embedder with word and bigram features."""
    async def gw(texts, model, trace_id=None):
        out = []
        for t in texts:
            v = [0.0] * dim
            words = _W.findall(t.lower())
            for f in words + [a + "_" + b for a, b in zip(words, words[1:])]:
                h = int(hashlib.md5(f.encode()).hexdigest(), 16)
                v[h % dim] += 1.0 if (h >> 64) & 1 else -1.0
            if not any(v):
                v[0] = 1.0
            out.append(v)
        return out, {"model_version": "offline-hash-768", "runtime": "local"}
    return gw


def pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 2) if xs else None


def norm(s):
    return " ".join(_W.findall(s.lower()))


async def gold_eval(mgr, rec_files, k=10):
    gold = json.load(open(GOLD))["questions"]
    admin = retrieval_filter(Principal("bench", ("platform_admin",), "user"))
    ranks = []
    for q in gold:
        rows, _ = await mgr.query(q["question"], k, where=admin)
        ev = [norm(e) for e in q["evidence"]]
        rank = None
        for i, r in enumerate(rows, 1):
            if rec_files.get(r["chunk_id"]) in q["files"] and all(e in norm(r["text"]) for e in ev):
                rank = i
                break
        ranks.append(rank)
    n = len(ranks)
    return {"n": n, "recall@1": round(100 * sum(1 for r in ranks if r == 1) / n, 1),
            "recall@5": round(100 * sum(1 for r in ranks if r and r <= 5) / n, 1),
            "mrr@10": round(sum(1 / r for r in ranks if r) / n, 3)}


async def main_async(args):
    if args.offline:
        embeddings._gateway = offline_gateway()
    reg = Registry(f"sqlite:///{os.path.join(args.corpus, 'registry.db')}")
    schema = "bench_" + uuid.uuid4().hex[:8]
    store = PgVectorStore(args.pg_dsn, schema=schema)
    await store.open()
    report = {"mode": "offline" if args.offline else "gateway", "pgvector": store.pgvector_version,
              "hnsw": {"m": config.HNSW_M, "ef_construction": config.HNSW_EF_CONSTRUCTION,
                       "ef_search": config.HNSW_EF_SEARCH}}
    try:
        mgr = IndexManager(store, reg)
        files = {}
        for s in reg.list_sources("active"):
            for c in reg.chunks_of(s["version_id"]):
                files[c["chunk_id"]] = s["metadata"]["file_name"]
        report["expected_chunks"] = len(mgr.expected())

        rec = await mgr.start_build(note="bench build")
        emb = mgr.embedder()
        t = time.time()
        built = await mgr.run_build(rec.index_id, activate=True)
        report["build"] = {"index": built.index_id, "status": built.status, "fingerprint": built.fingerprint,
                           "seconds": round(time.time() - t, 1), **built.stats}
        report["validation"] = {k: v for k, v in built.validation.items() if not k.endswith("_sample")}

        calls0 = emb.calls
        rec2 = await mgr.start_build(note="rebuild, unchanged corpus")
        t = time.time()
        rebuilt = await mgr.run_build(rec2.index_id)
        report["rebuild_unchanged"] = {"status": rebuilt.status, "seconds": round(time.time() - t, 1),
                                       "document_gateway_calls": emb.calls - calls0 - rebuilt.validation.get("sampled", 0) - 1,
                                       "cache_hits": rebuilt.stats.get("cache_hits")}
        await mgr.retire(rec2.index_id)

        # ANN vs exact, and latency, on 100 query vectors taken from chunk texts.
        gold = json.load(open(GOLD))["questions"]
        texts = [q["question"] for q in gold]
        sample_chunks = sorted(files)[:: max(1, len(files) // 60)][:60]
        chunk_text = {}
        for s in reg.list_sources("active"):
            for c in reg.chunks_of(s["version_id"]):
                if c["chunk_id"] in sample_chunks:
                    chunk_text[c["chunk_id"]] = c["text"][:300]
        texts += list(chunk_text.values())
        student = retrieval_filter(Principal("bench", ("student",), "user"))
        t_tbl = store._table(built.index_id)
        overlaps, lat = [], []
        for q in texts:
            vec, _ = await emb.embed_query(q, expect=None)
            t = time.perf_counter()
            ann = await store.query(built.index_id, vec, 10, where=student)
            lat.append((time.perf_counter() - t) * 1000)
            clause, params = student.sql()
            v = "[" + ",".join(f"{x:.8g}" for x in vec) + "]"
            async with store._conn() as conn, conn.transaction():
                await conn.execute("SET LOCAL enable_indexscan = off")
                exact = await (await conn.execute(
                    f"SELECT chunk_id FROM {t_tbl} WHERE {clause} ORDER BY embedding <=> %s::vector "
                    f"LIMIT 10", [*params, v])).fetchall()
            ex = {r[0] for r in exact}
            overlaps.append(len(ex & {r["chunk_id"] for r in ann}) / max(len(ex), 1))
        report["ann_vs_exact"] = {"queries": len(texts), "mean_top10_overlap": round(sum(overlaps) / len(overlaps), 3),
                                  "min_top10_overlap": round(min(overlaps), 3)}
        report["latency_ms_filtered_student_query"] = {"p50": pct(lat, 0.5), "p95": pct(lat, 0.95)}
        plan = await store.explain(built.index_id, vec, 10, where=student)
        report["query_plan_uses_hnsw"] = "Index Scan using" in plan and "embedding_idx" in plan

        if not args.offline:
            report["gold"] = {f"prefix_{config.EMBED_PREFIX_SCHEME}": await gold_eval(mgr, files)}
            if args.compare_prefixes:
                config.EMBED_PREFIX_SCHEME = "none"
                mgr2 = IndexManager(store, reg)
                r3 = await mgr2.start_build(note="no prefixes")
                b3 = await mgr2.run_build(r3.index_id, activate=True)
                report["gold"]["prefix_none"] = await gold_eval(mgr2, files)
                report["gold"]["prefix_none_index"] = b3.index_id
        else:
            report["gold"] = "skipped: offline embeddings carry no semantics; run on the DGX through the gateway"
    finally:
        async with store._conn() as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await store.close()
    out = args.out or os.path.join(args.corpus, "index_bench_report.json")
    with open(out, "w") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--pg-dsn", required=True)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--compare-prefixes", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
