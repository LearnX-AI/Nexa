"""NEXA-M2-004 re-index and rollback drill on the real corpus.

Runs the operational procedure end to end against PostgreSQL + pgvector and
records a transcript with a pass or fail for every step:

 1. Build index A (model A) from every approved source and activate it.
 2. Record the top-5 answers to every gold question from A.
 3. Re-index procedure: reprocess one source with a new chunk config and
    re-index it; re-index the whole corpus again (must be idempotent and,
    for unchanged text, served from the cache).
 4. Build index B with model B in parallel while queries keep being answered
    from A (no downtime), validate it, activate it.
 5. Mixing prevention: vectors of the wrong dimension are refused by A's
    collection; a query embedded with model B against index A is refused.
 6. Roll back to A and confirm every gold question gets the same top-5 as in
    step 2, except where step 3 legitimately changed the chunks.
 7. Retire B.

On the DGX, with two real models registered in the gateway:
    python v1/tools/knowledge_index_drill.py --corpus DIR --pg-dsn DSN \\
        --model-a nexa-embed-default --model-b embed-cand-bge-m3 --out docs/knowledge/evidence
Here, with stand-in embedders:
    python v1/tools/knowledge_index_drill.py --corpus DIR --pg-dsn DSN --offline --out OUT
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time
import uuid

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from v1.identity.principal import Principal  # noqa: E402
from v1.knowledge import embeddings  # noqa: E402
from v1.knowledge.access import retrieval_filter  # noqa: E402
from v1.knowledge.embeddings import ModelChanged  # noqa: E402
from v1.knowledge.indexing import IndexError_, IndexManager, info_of  # noqa: E402
from v1.knowledge.ingestion.blobstore import BlobStore  # noqa: E402
from v1.knowledge.ingestion.pipeline import Pipeline  # noqa: E402
from v1.knowledge.ingestion.registry import Registry  # noqa: E402
from v1.knowledge.pgstore import PgVectorStore  # noqa: E402
from v1.knowledge.store import StoreError  # noqa: E402

GOLD = os.path.join(ROOT, "v1", "knowledge", "benchmark", "gold_questions.json")


class Transcript:
    def __init__(self):
        self.steps = []
        self.t0 = time.time()

    def step(self, name, ok, **detail):
        self.steps.append({"step": len(self.steps) + 1, "name": name, "result": "pass" if ok else "FAIL",
                           "t_seconds": round(time.time() - self.t0, 2), **detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {json.dumps(detail, default=str)[:200]}", flush=True)
        return ok


async def snapshot(store, index_id):
    """Fingerprint of an index's exact contents: every chunk id and its vector."""
    import hashlib
    ids = sorted(await store.chunk_ids(index_id))
    vecs = await store.get_vectors(index_id, ids)
    h = hashlib.sha256()
    for cid in ids:
        h.update(cid.encode())
        h.update(",".join(f"{x:.9g}" for x in vecs[cid]).encode())
    return {"chunks": len(ids), "sha256": h.hexdigest()}


async def answers(mgr, gold, who):
    out = {}
    for q in gold:
        rows, rec = await mgr.query(q["question"], 5, where=who)
        out[q["id"]] = [r["chunk_id"] for r in rows]
    return out, rec


async def main_async(args):
    if args.offline:
        sys.path.insert(0, os.path.dirname(__file__))
        from knowledge_embed_eval import offline_gateway
        cands = [{"logical": args.model_a, "ollama_tag": "nomic-embed-text:v1.5"},
                 {"logical": args.model_b, "ollama_tag": "bge-m3"}]
        embeddings._gateway = offline_gateway(cands)
    gold = json.load(open(GOLD))["questions"]
    work = tempfile.mkdtemp(prefix="nexa-drill-")
    # Work on a copy of the corpus registry: the drill reprocesses a source.
    shutil.copy(os.path.join(args.corpus, "registry.db"), os.path.join(work, "registry.db"))
    reg = Registry(f"sqlite:///{os.path.join(work, 'registry.db')}")
    pipe = Pipeline(reg, BlobStore(os.path.join(args.corpus, "originals")))
    schema = "drill_" + uuid.uuid4().hex[:8]
    store = PgVectorStore(args.pg_dsn, schema=schema)
    await store.open()
    tr = Transcript()
    who = retrieval_filter(Principal("drill", ("platform_admin",), "user"))
    try:
        mgr = IndexManager(store, reg)
        # 1
        a = await mgr.start_build(args.model_a, note="drill: index A")
        a = await mgr.run_build(a.index_id, activate=True)
        tr.step("build and activate index A", a.status == "active", index=a.index_id, fingerprint=a.fingerprint,
                tokenizer=a.tokenizer, validation={k: a.validation.get(k) for k in
                                                   ("missing", "stale", "self_recall_at_5")})
        # 2
        before, _ = await answers(mgr, gold, who)
        tr.step("record top-5 answers from A", len(before) == len(gold), questions=len(before))
        # 3 re-index procedure
        src = next(s for s in reg.list_sources("active") if s["metadata"].get("approval_status") == "approved"
                   and s["metadata"]["file_name"].endswith(".docx"))
        old_chunks = {c["chunk_id"] for c in reg.chunks_of(src["version_id"])}
        data = pipe.blobs.get(src["metadata"]["content_checksum"])
        man = {"metadata": {k: v for k, v in src["metadata"].items()
                            if k not in ("ingested_at", "file_name", "mime_type", "file_size_bytes", "page_count",
                                         "content_checksum", "authority_tier", "schema_version")},
               "chunking": {"target_tokens": 200, "max_tokens": 320}}
        j, _ = pipe.submit(src["metadata"]["file_name"], data, man, "drill")
        j = pipe.process(j["job_id"])
        await mgr.index_version(src["version_id"])
        new_chunks = await store.chunk_ids(a.index_id)
        replaced = old_chunks.isdisjoint(new_chunks) and {c["chunk_id"] for c in reg.chunks_of(src["version_id"])} \
            <= new_chunks
        v = await mgr.validate(a.index_id)
        tr.step("re-index one reprocessed source (new chunk config)", j["state"] == "succeeded" and replaced
                and v["passed"], source=src["source_id"], old_chunks=len(old_chunks),
                new_chunks=len(reg.chunks_of(src["version_id"])), validation_missing=v["missing"],
                validation_stale=v["stale"])
        emb_a = mgr.embedder(args.model_a)
        calls0, n_before = emb_a.calls, len(await store.chunk_ids(a.index_id))
        full = await mgr.reindex_all()
        n_after = len(await store.chunk_ids(a.index_id))
        tr.step("full re-index is idempotent and cached", n_before == n_after and emb_a.calls == calls0
                and full["failed_sources"] == 0, chunks=n_after, gateway_calls=emb_a.calls - calls0, **full)
        changed = {c["chunk_id"] for c in reg.chunks_of(src["version_id"])}
        before, _ = await answers(mgr, gold, who)  # baseline after the legitimate chunk change
        snap_before = await snapshot(store, a.index_id)
        # 4 parallel build of B while A serves
        b = await mgr.start_build(args.model_b, note="drill: index B")
        served, errors = 0, 0

        async def keep_querying():
            nonlocal served, errors
            while True:
                try:
                    _, rec = await mgr.query(gold[served % len(gold)]["question"], 5, where=who)
                    served += rec.index_id == a.index_id
                except Exception:
                    errors += 1
                await asyncio.sleep(0)

        poller = asyncio.create_task(keep_querying())
        b = await mgr.run_build(b.index_id)
        poller.cancel()
        tr.step("build index B in parallel while A keeps serving", b.status == "ready" and served > 0 and errors == 0,
                index=b.index_id, fingerprint=b.fingerprint, queries_served_by_A_during_build=served,
                query_errors=errors, build_stats=b.stats)
        b = await mgr.activate(b.index_id)
        after_b, rec = await answers(mgr, gold, who)
        tr.step("activate B; queries now answered by B", rec.index_id == b.index_id and
                b.previous_index_id == a.index_id, active=rec.index_id, previous=b.previous_index_id)
        # 5 mixing prevention
        msg = ""
        if a.dim != b.dim:
            try:
                await store.upsert(a.index_id, [{"chunk_id": "x", "version_id": "x", "text": "x",
                                                 "vector": [0.1] * b.dim, "metadata": {}}])
                refused_dim = False
            except StoreError as exc:
                refused_dim, msg = True, str(exc)
            tr.step("collection A refuses vectors of another model's dimension", refused_dim, error=msg[:120])
        else:
            tr.step("collection A refuses vectors of another model's dimension", True,
                    note="models share a dimension; the fingerprint check in the next step still applies")
        try:
            await mgr.embedder(args.model_b).embed_query("photosynthesis", expect=info_of(a))
            refused_model = False
        except ModelChanged as exc:
            refused_model, msg = True, str(exc)
        tr.step("a query embedded with model B is refused by index A", refused_model,
                error=msg[:160] if refused_model else None)
        # 6 rollback
        await mgr.rollback()
        after_rb, rec = await answers(mgr, gold, who)
        snap_after = await snapshot(store, a.index_id)
        tr.step("roll back to A; index contents bit-identical to before the switch",
                rec.index_id == a.index_id and snap_after == snap_before,
                active=rec.index_id, before=snap_before, after=snap_after)
        same = sum(after_rb[q] == before[q] for q in before)
        tr.step("after rollback, every gold question gets the same top-5 as before the switch",
                same == len(before), identical_answers=f"{same}/{len(before)}",
                changed_chunks_in_step_3=len(changed))
        # 7 retire
        r = await mgr.retire(b.index_id)
        try:
            await mgr.retire(a.index_id)
            active_protected = False
        except IndexError_:
            active_protected = True
        tr.step("retire B; the active index cannot be retired", r.status == "retired" and active_protected,
                retired=b.index_id)
        indexes = [x.to_dict() for x in await store.list_indexes()]
    finally:
        async with store._conn() as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await store.close()
        shutil.rmtree(work, ignore_errors=True)
    report = {"mode": "offline" if args.offline else "gateway",
              "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "model_a": args.model_a, "model_b": args.model_b,
              "passed": all(s["result"] == "pass" for s in tr.steps), "steps": tr.steps, "indexes": indexes}
    os.makedirs(args.out, exist_ok=True)
    name = "m2-004-reindex-rollback-drill" + ("-offline" if args.offline else "") + ".json"
    with open(os.path.join(args.out, name), "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"drill {'PASSED' if report['passed'] else 'FAILED'}: {len(tr.steps)} steps")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--pg-dsn", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model-a", default="nexa-embed-default")
    ap.add_argument("--model-b", default="embed-cand-bge-m3")
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    report = asyncio.run(main_async(args))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
