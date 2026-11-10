"""NEXA-M2-005 retrieval benchmark, and NEXA-M2-006 evidence calibration.

    # on the DGX, through the Model Gateway
    python v1/tools/knowledge_retrieval_bench.py --corpus DIR --pg-dsn DSN \\
        --model nexa-embed-default --v0-model nexa-embed-default --out docs/knowledge/evidence
    # here, with stand-in embedders (only lexical numbers mean anything)
    python v1/tools/knowledge_retrieval_bench.py --corpus DIR --pg-dsn DSN --offline --out OUT

Builds, in a scratch schema, the v1 index (v1 chunks, the chosen model) and a
v0 baseline index (the v0 chunking from v0_baseline.py, embedded with the v0
model, queried by vector only, as main.py did). Then runs the gold set
(English and Tok Pisin) through:

  v0_vector                  the v0 baseline
  v1_vector                  v1 chunks, vector only (the M2-004 path)
  v1_lexical                 full-text only
  v1_hybrid                  vector + lexical, RRF
  v1_hybrid_rerank           + feature reranker
  v1_hybrid_filtered         hybrid with the subject and grade filters a tutor
                             knows from the course
  v1_hybrid_rerank_filtered
  edge_hybrid, edge_hybrid_rerank   the edge profile (fewer candidates)

For each: recall@1/5, MRR@10 per language, the selection score S, paired
bootstrap against v0 and against v1_vector, and stage latency p50/p95. The
reranker is recommended on by default only if it improves S by at least 1
point with P(better) >= 0.90 and lowers neither language's recall@5.

Calibration: the top passage's similarity and word coverage for answerable
questions found in the top 5, and for the unanswerable set
(unanswerable_questions.json), with suggested evidence thresholds.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sys
import uuid

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

from v1.identity.principal import Principal  # noqa: E402
from v1.knowledge import config, embeddings  # noqa: E402
from v1.knowledge.access import retrieval_filter  # noqa: E402
from v1.knowledge.benchmark import selection  # noqa: E402
from v1.knowledge.benchmark.v0_baseline import v0_chunks  # noqa: E402
from v1.knowledge.indexing import IndexManager, info_of  # noqa: E402
from v1.knowledge.ingestion.blobstore import BlobStore  # noqa: E402
from v1.knowledge.ingestion.registry import Registry  # noqa: E402
from v1.knowledge.pgstore import PgVectorStore  # noqa: E402
from v1.knowledge.retrieval import RetrievalRequest, Retriever  # noqa: E402
from v1.knowledge.store import IndexRecord, InMemoryStore  # noqa: E402

BENCH = os.path.join(ROOT, "v1", "knowledge", "benchmark")
_W = re.compile(r"[a-z0-9]+")
CONFIGS = [
    ("v1_vector", {"mode": "vector"}, False),
    ("v1_lexical", {"mode": "lexical"}, False),
    ("v1_hybrid", {"mode": "hybrid", "rerank": False}, False),
    ("v1_hybrid_rerank", {"mode": "hybrid", "rerank": True}, False),
    ("v1_hybrid_filtered", {"mode": "hybrid", "rerank": False}, True),
    ("v1_hybrid_rerank_filtered", {"mode": "hybrid", "rerank": True}, True),
    ("edge_hybrid", {"mode": "hybrid", "rerank": False, "profile": "edge"}, False),
    ("edge_hybrid_rerank", {"mode": "hybrid", "rerank": True, "profile": "edge"}, False),
]


def norm(s: str) -> str:
    return " ".join(_W.findall(s.lower()))


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1) if xs else None


def metrics_of(ranks: dict[str, list]) -> dict:
    out = {}
    for lang in ("en", "tpi"):
        rs = ranks[lang]
        out[lang] = {"n": len(rs), "recall@1": round(selection.recall_at(rs, 1), 1),
                     "recall@5": round(selection.recall_at(rs, 5), 1),
                     "mrr@10": round(sum(1 / r for r in rs if r) / len(rs), 3) if rs else None}
    out["score"] = round(selection.score(ranks["en"], ranks["tpi"]), 1)
    return out


def hit_rank(rows, q, file_of) -> int | None:
    ev = [norm(e) for e in q["evidence"]]
    for i, r in enumerate(rows, 1):
        if file_of(r) in q["files"] and all(e in norm(r["text"]) for e in ev):
            return i
    return None


async def build_v0(store, reg, blobs, emb, info) -> IndexRecord:
    rec = IndexRecord(index_id="idx_v0_" + uuid.uuid4().hex[:8], logical=info.logical, version=info.version,
                      runtime=info.runtime, dim=info.dim, prefix_scheme=info.prefix_scheme, note="v0 baseline")
    await store.create_index(rec)
    for s in reg.list_sources("active"):
        if s["metadata"].get("approval_status") != "approved":
            continue
        fn = s["metadata"]["file_name"]
        data = blobs.get(s["metadata"]["content_checksum"])
        if data is None or not fn.lower().endswith((".pdf", ".docx")):
            continue  # v0 loaded only PDF and DOCX
        chunks = [c for c in v0_chunks(fn, data, {"file": fn}) if c["text"].strip()]
        if not chunks:
            continue
        vecs, _ = await emb.embed_documents([c["text"] for c in chunks], expect=info)
        await store.upsert(rec.index_id, [
            {"chunk_id": f"v0_{s['version_id'][-16:]}_{i}".replace("@", "_").replace(":", "_")[:120],
             "version_id": s["version_id"], "text": c["text"], "vector": v, "metadata": {"file": fn}}
            for i, (c, v) in enumerate(zip(chunks, vecs))])
    return rec


def suggest_thresholds(hits: list[dict], unans: list[dict]) -> dict:
    """min: the similarity cut that best separates unanswerable questions (want
    'none') from answerable ones found in the top 5 (want not 'none'), with
    the word-coverage rule applied as in grounding.assess. strong: the 25th
    percentile of answerable similarities, so most good answers are strong."""
    cov_strong = config.EVIDENCE_COVERAGE_STRONG
    sims = sorted({round(x["similarity"], 3) for x in hits + unans if x["similarity"] is not None})
    best, best_acc = None, -1.0
    for t in sims:
        ok = sum(1 for x in unans if x["similarity"] < t and x["coverage"] < cov_strong)
        ok += sum(1 for x in hits if x["similarity"] >= t or x["coverage"] >= cov_strong)
        acc = ok / max(len(hits) + len(unans), 1)
        if acc > best_acc:
            best, best_acc = t, acc
    hs = sorted(x["similarity"] for x in hits if x["similarity"] is not None)
    strong = hs[len(hs) // 4] if hs else None
    return {"min": best, "strong": max(strong, best) if strong is not None and best is not None else strong,
            "separation_accuracy": round(best_acc, 3)}


async def main_async(args) -> dict:
    gold = json.load(open(os.path.join(BENCH, "gold_questions.json")))["questions"]
    unanswerable = json.load(open(os.path.join(BENCH, "unanswerable_questions.json")))["questions"]
    if args.offline:
        from knowledge_embed_eval import offline_gateway
        embeddings._gateway = offline_gateway([{"logical": args.model, "ollama_tag": "nomic-embed-text:v1.5"},
                                               {"logical": args.v0_model, "ollama_tag": "nomic-embed-text:v1.5"}])
    reg = Registry(f"sqlite:///{os.path.join(args.corpus, 'registry.db')}")
    blobs = BlobStore(os.path.join(args.corpus, "originals"))
    files, src_md = {}, {}
    for s in reg.list_sources("active"):
        if s["metadata"].get("approval_status") != "approved":
            continue
        src_md[s["metadata"]["file_name"]] = s["metadata"]
        for c in reg.chunks_of(s["version_id"]):
            files[c["chunk_id"]] = s["metadata"]["file_name"]
    schema = "retr_bench_" + uuid.uuid4().hex[:8]
    store = PgVectorStore(args.pg_dsn, schema=schema) if args.pg_dsn else InMemoryStore()
    await store.open()
    who = retrieval_filter(Principal("bench", ("platform_admin",), "user"))
    report: dict = {"mode": "offline" if args.offline else "gateway", "store": store.name,
                    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "gold": {"en": sum(q.get("language", "en") == "en" for q in gold),
                             "tpi": sum(q.get("language") == "tpi" for q in gold)},
                    "unanswerable": len(unanswerable), "configs": {}}
    try:
        mgr = IndexManager(store, reg)
        rec = await mgr.start_build(args.model, note="M2-005 benchmark")
        rec = await mgr.run_build(rec.index_id, activate=True)
        report["v1_index"] = {"index_id": rec.index_id, "fingerprint": rec.fingerprint, "chunks": len(files),
                              "validation_passed": rec.validation.get("passed")}
        v0emb = mgr.embedder(args.v0_model)
        v0info = await v0emb.describe()
        v0 = await build_v0(store, reg, blobs, v0emb, v0info)
        report["v0_index"] = {"index_id": v0.index_id, "fingerprint": v0.fingerprint,
                              "chunks": len(await store.chunk_ids(v0.index_id))}
        retr = Retriever(mgr)

        # v0 baseline
        ranks = {"en": [], "tpi": []}
        per_q, lat = {}, []
        for q in gold:
            t = asyncio.get_running_loop().time()
            qv, _ = await v0emb.embed_query(q["question"], expect=info_of(v0))
            rows = await store.query(v0.index_id, qv, 10)
            lat.append((asyncio.get_running_loop().time() - t) * 1000)
            rk = hit_rank(rows, q, lambda r: (r.get("metadata") or {}).get("file"))
            ranks[q.get("language", "en")].append(rk)
            per_q[q["id"]] = rk
        report["configs"]["v0_vector"] = {**metrics_of(ranks), "en_ranks": ranks["en"], "tpi_ranks": ranks["tpi"],
                                          "per_question": per_q, "latency_ms": {"total_p50": pct(lat, .5),
                                                                                "total_p95": pct(lat, .95)}}

        calib_hits, calib_unans = [], []
        for name, kw, filtered in CONFIGS:
            ranks = {"en": [], "tpi": []}
            per_q, tot, nonembed = {}, [], []
            stage: dict[str, list] = {}
            for q in gold:
                flt = {}
                if filtered:
                    mds = [src_md[f] for f in q["files"] if f in src_md]
                    flt = {"subjects": sorted({m["subject"] for m in mds}),
                           "grades": sorted({g for m in mds for g in m["grade_levels"]})}
                rr = await retr.retrieve(RetrievalRequest(q["question"], 10, filters=flt, **kw), who)
                rk = hit_rank(rr.results, q, lambda r: files.get(r["chunk_id"]))
                ranks[q.get("language", "en")].append(rk)
                per_q[q["id"]] = rk
                tm = rr.explain["timings_ms"]
                tot.append(tm["total"])
                nonembed.append(tm["total"] - tm.get("embed", 0.0))
                for k, v in tm.items():
                    stage.setdefault(k, []).append(v)
                if name == "v1_hybrid" and rk and rk <= 5 and rr.results:
                    calib_hits.append({"id": q["id"], "similarity": rr.results[0]["scores"].get("similarity"),
                                       "coverage": rr.results[0]["coverage"]["coverage"]})
            report["configs"][name] = {
                **metrics_of(ranks), "en_ranks": ranks["en"], "tpi_ranks": ranks["tpi"], "per_question": per_q,
                "latency_ms": {"total_p50": pct(tot, .5), "total_p95": pct(tot, .95),
                               "excluding_embed_p50": pct(nonembed, .5), "excluding_embed_p95": pct(nonembed, .95),
                               "stages_p50": {k: pct(v, .5) for k, v in stage.items()}}}
        for q in unanswerable:
            rr = await retr.retrieve(RetrievalRequest(q["question"], 5, mode="hybrid", rerank=False), who)
            top = rr.results[0] if rr.results else None
            calib_unans.append({"id": q["id"], "similarity": top["scores"].get("similarity") if top else None,
                                "coverage": top["coverage"]["coverage"] if top else 0.0})
        report["calibration"] = {"answerable_found": calib_hits, "unanswerable": calib_unans,
                                 "current_thresholds": config.EVIDENCE_THRESHOLDS,
                                 "suggested": suggest_thresholds(calib_hits, [u for u in calib_unans
                                                                              if u["similarity"] is not None])}
    finally:
        if store.name == "pgvector":
            async with store._conn() as conn:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await store.close()

    c = report["configs"]
    cmp = {}
    for a, b in [("v1_hybrid", "v0_vector"), ("v1_vector", "v0_vector"), ("v1_hybrid", "v1_vector"),
                 ("v1_hybrid_rerank", "v1_hybrid"), ("v1_hybrid_filtered", "v1_hybrid"),
                 ("v1_hybrid_rerank", "v0_vector"), ("edge_hybrid", "v1_hybrid")]:
        cmp[f"{a} vs {b}"] = {"delta_score": round(c[a]["score"] - c[b]["score"], 1),
                              "p_better": selection.p_better(c[a], c[b])}
    report["comparisons"] = cmp
    rr_ = cmp["v1_hybrid_rerank vs v1_hybrid"]
    better = (rr_["delta_score"] >= 1.0 and rr_["p_better"] >= 0.90
              and c["v1_hybrid_rerank"]["en"]["recall@5"] >= c["v1_hybrid"]["en"]["recall@5"]
              and c["v1_hybrid_rerank"]["tpi"]["recall@5"] >= c["v1_hybrid"]["tpi"]["recall@5"])
    report["recommendation"] = {
        "rerank_default": "on" if better else "off",
        "reason": (f"reranker changes S by {rr_['delta_score']:+.1f} (P(better) {rr_['p_better']}); the rule needs "
                   ">= +1.0 with P >= 0.90 and no language losing recall@5"),
        "hybrid_vs_v0": cmp["v1_hybrid vs v0_vector"],
    }
    os.makedirs(args.out, exist_ok=True)
    stem = "m2-005-retrieval-benchmark" + ("-offline" if args.offline else "")
    with open(os.path.join(args.out, stem + ".json"), "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    md = markdown(report)
    with open(os.path.join(args.out, stem + ".md"), "w") as fh:
        fh.write(md)
    print(md)
    return report


def markdown(r: dict) -> str:
    lines = [f"# NEXA-M2-005 retrieval benchmark ({r['mode']}, {r['store']})", "",
             f"Run {r['generated_at']}. v1 index {r['v1_index']['fingerprint']} ({r['v1_index']['chunks']} chunks); "
             f"v0 baseline {r['v0_index']['fingerprint']} ({r['v0_index']['chunks']} v0 chunks). "
             f"Gold set {r['gold']['en']} English + {r['gold']['tpi']} Tok Pisin.", ""]
    if r["mode"] == "offline":
        lines += ["> **Stand-in embedders.** Vector and hybrid numbers here say nothing about real models; "
                  "lexical numbers are real. Run on the DGX for the evidence.", ""]
    lines += ["| Config | EN R@1 | EN R@5 | TPI R@5 | MRR@10 EN | S | p50 ms | p95 ms | p95 excl. embed |",
              "|---|---|---|---|---|---|---|---|---|"]
    for name, c in r["configs"].items():
        lat = c["latency_ms"]
        lines.append(f"| {name} | {c['en']['recall@1']} | {c['en']['recall@5']} | {c['tpi']['recall@5']} | "
                     f"{c['en']['mrr@10']} | {c['score']} | {lat.get('total_p50')} | {lat.get('total_p95')} | "
                     f"{lat.get('excluding_embed_p95', '-')} |")
    lines += ["", "| Comparison | ΔS | P(better) |", "|---|---|---|"]
    for k, v in r["comparisons"].items():
        lines.append(f"| {k} | {v['delta_score']:+.1f} | {v['p_better']} |")
    s = r["calibration"]["suggested"]
    lines += ["", f"**Reranker default:** {r['recommendation']['rerank_default']}. {r['recommendation']['reason']}.",
              "", f"**Evidence thresholds suggested:** min {s['min']}, strong {s['strong']} "
                  f"(separation accuracy {s['separation_accuracy']}).", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--pg-dsn", default="", help="PostgreSQL with pgvector; empty uses the in-memory store")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="nexa-embed-default")
    ap.add_argument("--v0-model", default="nexa-embed-default", help="v0 used nomic-embed-text with prefixes")
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
