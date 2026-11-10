"""NEXA-M2-004 embedding model evaluation on the NEXA curriculum benchmark.

On the DGX (real models, through the Model Gateway):

    # 1. ollama pull each tag in v1/knowledge/benchmark/embedding_candidates.json
    # 2. merge v1/model_gateway/models.embedding-candidates.json into the gateway registry
    NEXA_KNOWLEDGE_GATEWAY_TOKEN=... NEXA_GATEWAY_URL=http://127.0.0.1:8100 \\
    python v1/tools/knowledge_embed_eval.py --corpus DIR --pg-dsn "$DSN" --out docs/knowledge/evidence

Self-test of the harness without models (deterministic stand-in embedders;
the numbers mean nothing about real models):

    python v1/tools/knowledge_embed_eval.py --corpus DIR --pg-dsn "$DSN" --offline --out OUT

For every candidate it builds an index version beside the others in one scratch
schema (parallel index versions), validates it, then measures on the gold set
(v1/knowledge/benchmark/gold_questions.json; English and Tok Pisin questions):
recall@1/5, MRR@10 per language and category, query embedding latency,
indexing throughput, and the share of chunks longer than the model's input
limit. The selection rule in v1/knowledge/benchmark/selection.py then picks the
baseline. Writes m2-004-embedding-eval.json and m2-004-embedding-eval.md.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
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
from v1.knowledge import embeddings  # noqa: E402
from v1.knowledge.access import retrieval_filter  # noqa: E402
from v1.knowledge.benchmark import selection  # noqa: E402
from v1.knowledge.embeddings import card_for, prefix_scheme_for  # noqa: E402
from v1.knowledge.indexing import IndexManager, info_of  # noqa: E402
from v1.knowledge.ingestion.registry import Registry  # noqa: E402
from v1.knowledge.pgstore import PgVectorStore  # noqa: E402
from v1.knowledge.processing.chunking import count_tokens  # noqa: E402

BENCH = os.path.join(ROOT, "v1", "knowledge", "benchmark")
# The chunker's token estimate counts words and punctuation. Subword tokenizers
# produce more pieces; 1.3 is a conservative factor for curriculum English.
SUBWORD_FACTOR = 1.3
_W = re.compile(r"[a-z0-9]+")


def norm(s: str) -> str:
    return " ".join(_W.findall(s.lower()))


def pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1) if xs else None


# ------------------------------------------------------------ offline stand-ins

_TPI = {"wanem": "what", "bilong": "of", "samting": "thing", "hamas": "how many", "kain": "kind",
        "pisin": "birds", "opim": "unwinds", "mekim": "makes", "kamap": "produced", "senisim": "replaced",
        "kol": "cold", "liklik": "less", "ogen": "organ", "blut": "blood", "lukautim": "control",
        "nogut": "disadvantage", "skelim": "compared", "sikispela": "six", "kopi": "copied", "taim": "time",
        "wok": "work", "longpela": "long", "ol": "", "i": "", "long": "", "na": "and", "em": "", "save": ""}


def offline_gateway(candidates):
    """Stand-in embedders, one per candidate: hashing at the candidate's real
    dimension; candidates marked multilingual also map a small Tok Pisin
    lexicon. For exercising the harness only."""
    dims = {"bge-m3": 1024, "snowflake-arctic-embed2": 1024, "qwen3-embedding:0.6b": 1024,
            "qwen3-embedding:4b": 2560, "mxbai-embed-large": 1024, "all-minilm:33m": 384,
            "granite-embedding:278m": 768}
    by_logical = {c["logical"]: c for c in candidates}

    async def gw(texts, model, trace_id=None):
        c = by_logical.get(model) or {"ollama_tag": "nomic-embed-text:v1.5"}
        tag = c["ollama_tag"]
        dim = dims.get(tag, 768)
        card = card_for(tag)
        multi = bool(card and card.multilingual)
        out = []
        for t in texts:
            words = _W.findall(t.lower())
            if multi:
                words = " ".join(_TPI.get(w, w) for w in words).split()
            v = [0.0] * dim
            for f in words + [a + "_" + b for a, b in zip(words, words[1:])]:
                h = int(hashlib.md5((tag + f).encode()).hexdigest(), 16)
                v[h % dim] += 1.0 if (h >> 64) & 1 else -1.0
            if not any(v):
                v[0] = 1.0
            out.append(v)
        return out, {"model_version": tag, "runtime": "offline-standin"}
    return gw


# ------------------------------------------------------------ evaluation

async def evaluate(mgr: IndexManager, store: PgVectorStore, cand: dict, gold: list, files: dict,
                   chunk_texts: list[str]) -> dict:
    logical = cand["logical"]
    res = {"logical": logical, "ollama_tag": cand["ollama_tag"], "role": cand["role"]}
    t0 = time.time()
    try:
        rec = await mgr.start_build(logical, note=f"M2-004 evaluation: {cand['ollama_tag']}")
    except Exception as exc:  # model not pulled, not registered, gateway down
        res.update(index_status="unavailable", error=str(exc)[:300], en_ranks=[], tpi_ranks=[], dim=0,
                   card=None, validation_passed=False, truncated_pct=100.0)
        return res
    built = await mgr.run_build(rec.index_id)
    res.update(index_id=built.index_id, index_status=built.status, fingerprint=built.fingerprint,
               dim=built.dim, tokenizer=built.tokenizer, max_input_tokens=built.max_input_tokens,
               build_seconds=round(time.time() - t0, 1),
               chunks_per_second=round(len(chunk_texts) / max(time.time() - t0, 1e-6), 1),
               validation_passed=bool(built.validation.get("passed")),
               self_recall_at_5=built.validation.get("self_recall_at_5"))
    card = card_for(built.version)
    res["card"] = card.__dict__ if card else None
    _, doc_prefix, _ = prefix_scheme_for(built.version)
    limit = card.max_input_tokens if card else 512
    over = sum(1 for t in chunk_texts if count_tokens(doc_prefix + t) * SUBWORD_FACTOR > limit)
    res["truncated_pct"] = round(100.0 * over / max(len(chunk_texts), 1), 1)
    if built.status != "ready":
        res.update(en_ranks=[], tpi_ranks=[])
        return res

    emb = mgr.embedder(logical)
    who = retrieval_filter(Principal("eval", ("platform_admin",), "user"))
    ranks: dict[str, list] = {"en": [], "tpi": []}
    per_q, lat = {}, []
    by_cat: dict[str, list] = {}
    for q in gold:
        t = time.perf_counter()
        vec, _ = await emb.embed_query(q["question"], expect=info_of(built))
        lat.append((time.perf_counter() - t) * 1000)
        rows = await store.query(built.index_id, vec, 10, where=who)
        ev = [norm(e) for e in q["evidence"]]
        rank = next((i for i, r in enumerate(rows, 1)
                     if files.get(r["chunk_id"]) in q["files"] and all(e in norm(r["text"]) for e in ev)), None)
        lang = q.get("language", "en")
        ranks[lang].append(rank)
        per_q[q["id"]] = rank
        by_cat.setdefault(f"{lang}:{q['category']}", []).append(rank)
    res.update(en_ranks=ranks["en"], tpi_ranks=ranks["tpi"], per_question=per_q,
               query_embed_ms={"p50": pct(lat, 0.5), "p95": pct(lat, 0.95)})
    for lang in ("en", "tpi"):
        rs = ranks[lang]
        res[lang] = {"n": len(rs), "recall@1": round(selection.recall_at(rs, 1), 1),
                     "recall@5": round(selection.recall_at(rs, 5), 1),
                     "mrr@10": round(sum(1 / r for r in rs if r) / len(rs), 3) if rs else None}
    res["by_category_recall@5"] = {k: round(selection.recall_at(v, 5), 1) for k, v in sorted(by_cat.items())}
    return res


def markdown(report: dict) -> str:
    d = report["decision"]
    lines = [
        f"# NEXA-M2-004 embedding evaluation ({report['mode']})", "",
        f"Run {report['generated_at']}; corpus {report['chunks']} chunks; gold set {report['gold']['en']} English "
        f"+ {report['gold']['tpi']} Tok Pisin questions; selection rule {d['rule_version']}.", "",
    ]
    if report["mode"] == "offline":
        lines += ["> **Self-test only.** Stand-in embedders, not real models. These numbers say nothing about "
                  "model quality; they show the harness, gates and rule work end to end.", ""]
    lines += ["| Model | Role | Dim | Status | EN R@5 | TPI R@5 | Score | Trunc % | Query p50 ms | Gates | Eligible |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in report["results"]:
        g = r.get("gates") or {}
        failed = ",".join(k.split("_")[0] for k, v in g.items() if not v) or "all pass"
        lines.append(f"| {r['ollama_tag']} | {r['role']} | {r.get('dim') or '-'} | {r['index_status']} | "
                     f"{(r.get('en') or {}).get('recall@5', '-')} | {(r.get('tpi') or {}).get('recall@5', '-')} | "
                     f"{r.get('score', '-')} | {r.get('truncated_pct', '-')} | "
                     f"{(r.get('query_embed_ms') or {}).get('p50', '-')} | {failed} | "
                     f"{'yes' if r.get('eligible') else 'no'} |")
    lines += ["", f"**Selected baseline:** `{d.get('selected')}`. {d.get('reason', '')}", ""]
    if d.get("challenger"):
        lines.append(f"Best eligible challenger: `{d['challenger']}` (margin {d.get('margin')}, "
                     f"P(better) {d.get('p_better')}).")
    return "\n".join(lines) + "\n"


async def main_async(args) -> dict:
    cands = json.load(open(os.path.join(BENCH, "embedding_candidates.json")))["candidates"]
    if args.only:
        cands = [c for c in cands if c["ollama_tag"] in args.only.split(",")]
    if args.offline:
        embeddings._gateway = offline_gateway(cands)
    gold = json.load(open(os.path.join(BENCH, "gold_questions.json")))["questions"]
    reg = Registry(f"sqlite:///{os.path.join(args.corpus, 'registry.db')}")
    files, chunk_texts = {}, []
    for s in reg.list_sources("active"):
        if s["metadata"].get("approval_status") != "approved":
            continue
        for c in reg.chunks_of(s["version_id"]):
            files[c["chunk_id"]] = s["metadata"]["file_name"]
            chunk_texts.append(c["embedding_text"])
    schema = "embed_eval_" + uuid.uuid4().hex[:8]
    store = PgVectorStore(args.pg_dsn, schema=schema)
    await store.open()
    results = []
    try:
        mgr = IndexManager(store, reg)
        for c in cands:
            print(f"evaluating {c['ollama_tag']} ...", flush=True)
            r = await evaluate(mgr, store, c, gold, files, chunk_texts)
            results.append(r)
            print(f"  {r['index_status']} en={r.get('en')} tpi={r.get('tpi')} trunc={r.get('truncated_pct')}",
                  flush=True)
        indexes = [x.to_dict() for x in await store.list_indexes()]
    finally:
        if not args.keep_schema:
            async with store._conn() as conn:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await store.close()
    decision = selection.decide(results)
    report = {
        "mode": "offline" if args.offline else "gateway",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "chunks": len(chunk_texts),
        "gold": {"en": sum(q.get("language", "en") == "en" for q in gold),
                 "tpi": sum(q.get("language") == "tpi" for q in gold)},
        "subword_factor": SUBWORD_FACTOR,
        "parallel_index_versions": indexes,
        "results": results,
        "decision": decision,
    }
    os.makedirs(args.out, exist_ok=True)
    stem = "m2-004-embedding-eval" + ("-offline-selftest" if args.offline else "")
    with open(os.path.join(args.out, stem + ".json"), "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    with open(os.path.join(args.out, stem + ".md"), "w") as fh:
        fh.write(markdown(report))
    print(markdown(report))
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, help="output directory of knowledge_corpus.py")
    ap.add_argument("--pg-dsn", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--only", default="", help="comma-separated Ollama tags to evaluate")
    ap.add_argument("--keep-schema", action="store_true")
    args = ap.parse_args()
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
