"""NEXA-M2-004: embedding service, index versions, pgvector store.

Store-contract and lifecycle tests run against the in-memory store always, and
against PostgreSQL + pgvector when NEXA_TEST_PG_DSN points at a database with
the vector extension (CI starts one). Each pgvector test uses its own schema.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from v1.identity.principal import Principal
from v1.knowledge import config, embeddings
from v1.knowledge import main as km
from v1.knowledge.access import retrieval_filter
from v1.knowledge.embeddings import Embedder, ModelChanged, prefix_scheme_for
from v1.knowledge.indexing import IndexError_, IndexManager
from v1.knowledge.ingestion.pipeline import Pipeline
from v1.knowledge.store import IndexRecord, InMemoryStore, StoreError

from .knowledge_fixtures import FakeGateway, bow_vector, lesson_manifest, make_docx

PG_DSN = os.environ.get("NEXA_TEST_PG_DSN", "")
BACKENDS = ["memory"] + (["pgvector"] if PG_DSN else [])


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def gw(monkeypatch):
    g = FakeGateway()
    monkeypatch.setattr(embeddings, "_gateway", g)
    return g


async def _open_store(kind):
    if kind == "memory":
        return InMemoryStore()
    from v1.knowledge.pgstore import PgVectorStore
    s = PgVectorStore(PG_DSN, schema="t_" + uuid.uuid4().hex[:10])
    await s.open()
    return s


async def _close_store(s):
    if s.name == "pgvector":
        async with s._conn() as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS {s.schema} CASCADE")
        await s.close()


def with_store(kind, fn):
    async def go():
        s = await _open_store(kind)
        try:
            return await fn(s)
        finally:
            await _close_store(s)
    return run(go())


# ---------------------------------------------------------------- embedder

def test_prefix_schemes():
    assert prefix_scheme_for("nomic-embed-text")[1:] == ("search_document: ", "search_query: ")
    assert prefix_scheme_for("nomic-embed-text:v1.5")[0] == "nomic-embed"
    assert prefix_scheme_for("intfloat/multilingual-e5-large")[0] == "multilingual-e5"
    assert prefix_scheme_for("text-embedding-3-small") == ("none", "", "")


def test_custom_and_disabled_prefixes(monkeypatch):
    monkeypatch.setattr(config, "EMBED_PREFIX_SCHEME", "custom")
    monkeypatch.setattr(config, "EMBED_DOC_PREFIX", "doc: ")
    monkeypatch.setattr(config, "EMBED_QUERY_PREFIX", "q: ")
    assert prefix_scheme_for("nomic-embed-text") == ("custom", "doc: ", "q: ")
    monkeypatch.setattr(config, "EMBED_PREFIX_SCHEME", "none")
    assert prefix_scheme_for("nomic-embed-text") == ("none", "", "")


def test_embedder_applies_prefixes_normalises_and_caches(gw):
    emb = Embedder("nexa-embed-default")
    vecs, info = run(emb.embed_documents(["Helicase unwinds DNA.", "Primase makes primers."]))
    assert info.fingerprint == "nexa-embed-default|nomic-embed-text|ollama|128|nomic-embed"
    assert gw.texts[-2:] == ["search_document: Helicase unwinds DNA.", "search_document: Primase makes primers."]
    assert all(abs(math.sqrt(sum(x * x for x in v)) - 1) < 1e-9 for v in vecs)
    calls = gw.calls
    again, _ = run(emb.embed_documents(["Helicase unwinds DNA."]))
    assert gw.calls == calls and again[0] == vecs[0] and emb.cache_hits == 1  # served from cache
    q, _ = run(emb.embed_query("what unwinds DNA"))
    assert gw.texts[-1] == "search_query: what unwinds DNA"


def test_embedder_batches(gw, monkeypatch):
    monkeypatch.setattr(config, "EMBED_BATCH", 3)
    emb = Embedder()
    run(emb.describe())
    before = gw.calls
    run(emb.embed_documents([f"text number {i}" for i in range(7)]))
    assert gw.calls - before == 3


@pytest.mark.parametrize("bad, msg", [
    ([float("nan")] * 128, "non-finite"),
    ([0.0] * 128, "all zeros"),
    ([1.0] * 64, "model changed"),  # a new dimension is a new model
])
def test_embedder_rejects_bad_vectors(monkeypatch, bad, msg):
    async def gateway(texts, model, trace_id=None):
        if texts == ["dimension probe"]:
            return [[1.0] * 128], {"model_version": "nomic-embed-text", "runtime": "ollama"}
        return [bad for _ in texts], {"model_version": "nomic-embed-text", "runtime": "ollama"}

    monkeypatch.setattr(embeddings, "_gateway", gateway)
    with pytest.raises(embeddings.EmbeddingError, match=msg):
        run(Embedder().embed_documents(["x"]))


def test_model_change_is_detected(gw):
    emb = Embedder()
    _, info = run(emb.embed_documents(["photosynthesis"]))
    gw.version = "nomic-embed-text:v2"
    with pytest.raises(ModelChanged, match="build and activate a new index"):
        run(emb.embed_query("chlorophyll", expect=info))


# ---------------------------------------------------------------- filter

def test_retrieval_filter_python_and_sql_agree():
    f = retrieval_filter(Principal("u", ("student",), "user"), on=dt.date(2025, 1, 1))
    ok = {"approval_status": "approved", "access_classification": "learner", "scope": "national",
          "effective_from": "2024-01-01"}
    assert f(ok) and f({"text": "raw M1 doc"})
    assert not f({**ok, "access_classification": "teacher_only"})
    assert not f({**ok, "scope": "school"}) and not f({**ok, "effective_from": "2026-01-01"})
    clause, params = f.sql()
    assert "scope NOT IN" in clause and params == [["learner", "public"], "2025-01-01", "2025-01-01"]
    admin = retrieval_filter(Principal("a", ("platform_admin",), "user"))
    assert "scope NOT IN" not in admin.sql()[0] and "restricted" in admin.sql()[1][0]


# ---------------------------------------------------------------- store contract

def _rec(iid="idx_test_a", dim=4):
    return IndexRecord(index_id=iid, logical="m", version="v", runtime="r", dim=dim, prefix_scheme="none")


def _item(cid, vec, version="src@1", **md):
    base = {"approval_status": "approved", "access_classification": "learner", "scope": "national",
            "effective_from": "2024-01-01"}
    return {"chunk_id": cid, "version_id": version, "text": f"text {cid}", "vector": vec,
            "metadata": {**base, **md}}


@pytest.mark.parametrize("kind", BACKENDS)
def test_store_contract(kind):
    async def body(s):
        await s.create_index(_rec())
        await s.create_index(_rec("idx_test_b"))
        with pytest.raises(StoreError, match="dimension"):
            await s.upsert("idx_test_a", [_item("c0", [1.0, 0.0])])
        await s.upsert("idx_test_a", [
            _item("c1", [1, 0, 0, 0]), _item("c2", [0.9, 0.1, 0, 0]),
            _item("c3", [0, 1, 0, 0], access_classification="teacher_only"),
            _item("c4", [0.8, 0.2, 0, 0], version="src@2"),
        ])
        await s.upsert("idx_test_a", [_item("c1", [1, 0, 0, 0], citation="updated")])  # upsert, no duplicate
        assert await s.chunk_ids("idx_test_a") == {"c1", "c2", "c3", "c4"}
        student = retrieval_filter(Principal("u", ("student",), "user"), on=dt.date(2025, 1, 1))
        rows = await s.query("idx_test_a", [1, 0, 0, 0], 3, where=student)
        assert [r["chunk_id"] for r in rows] == ["c1", "c2", "c4"]
        assert rows[0]["metadata"]["citation"] == "updated" and rows[0]["score"] == pytest.approx(1.0)
        teacher = retrieval_filter(Principal("t", ("teacher",), "user"), on=dt.date(2025, 1, 1))
        assert "c3" in {r["chunk_id"] for r in await s.query("idx_test_a", [0, 1, 0, 0], 4, where=teacher)}
        assert await s.delete_version("src@2") == 1
        assert await s.delete_chunks("idx_test_a", ["c2", "nope"]) == 1
        assert await s.chunk_ids("idx_test_a") == {"c1", "c3"}
        assert (await s.get_vectors("idx_test_a", ["c1"]))["c1"] == pytest.approx([1, 0, 0, 0])
        # alias switching
        assert await s.active_index_id() is None
        a = await s.set_active("idx_test_a")
        assert a.status == "active" and a.previous_index_id is None
        b = await s.set_active("idx_test_b")
        assert b.previous_index_id == "idx_test_a" and (await s.get_index("idx_test_a")).status == "ready"
        with pytest.raises(StoreError, match="active"):
            await s.drop_index("idx_test_b")
        await s.drop_index("idx_test_a")
        assert (await s.get_index("idx_test_a")).status == "retired"
        # cache
        await s.put_many("fp", {"k1": [0.5, 0.5]})
        assert await s.get_many("fp", ["k1", "k2"]) == {"k1": pytest.approx([0.5, 0.5])}
        assert await s.get_many("other", ["k1"]) == {}
    with_store(kind, body)


@pytest.mark.skipif(not PG_DSN, reason="NEXA_TEST_PG_DSN not set")
def test_pgvector_persists_across_reopen():
    from v1.knowledge.pgstore import PgVectorStore

    schema = "t_" + uuid.uuid4().hex[:10]

    async def body():
        s = PgVectorStore(PG_DSN, schema=schema)
        await s.open()
        await s.create_index(_rec())
        await s.upsert("idx_test_a", [_item("c1", [1, 0, 0, 0])])
        await s.set_active("idx_test_a")
        await s.close()
        s2 = PgVectorStore(PG_DSN, schema=schema)  # a service restart
        await s2.open()
        try:
            assert await s2.active_index_id() == "idx_test_a"
            assert [r["chunk_id"] for r in await s2.query("idx_test_a", [1, 0, 0, 0], 1)] == ["c1"]
            async with s2._conn() as conn:
                idx = await (await conn.execute(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND indexdef LIKE %s",
                    (schema, "%hnsw%"))).fetchall()
            assert idx and "vector_cosine_ops" in idx[0][0]
            # The store's own query shape must be able to use the HNSW index.
            import random
            rnd = random.Random(7)
            await s2.create_index(IndexRecord("idx_test_plan", "m", "v", "r", 32, "none"))
            await s2.upsert("idx_test_plan", [_item(f"p{i}", [rnd.gauss(0, 1) for _ in range(32)]) for i in range(2000)])
            async with s2._conn() as conn:
                await conn.execute(f"ANALYZE {schema}.vec_idx_test_plan")
            student = retrieval_filter(Principal("u", ("student",), "user"))
            plan = await s2.explain("idx_test_plan", [rnd.gauss(0, 1) for _ in range(32)], 10, where=student)
            assert "Index Scan using" in plan and "embedding_idx" in plan, plan
        finally:
            async with s2._conn() as conn:
                await conn.execute(f"DROP SCHEMA {schema} CASCADE")
            await s2.close()
    run(body())


def test_pgvector_rejects_unsafe_identifiers():
    from v1.knowledge.pgstore import PgVectorStore
    with pytest.raises(StoreError):
        PgVectorStore("postgresql://x", schema="knowledge; drop table x")
    s = PgVectorStore("postgresql://x")
    with pytest.raises(StoreError, match="invalid index id"):
        s._table("idx'; drop table knowledge.sources; --")


# ---------------------------------------------------------------- lifecycle

def _ingest(pipe, item="cm9001", **extra):
    data = make_docx(extra_paragraphs=4, title=item) if not extra.pop("other", False) else \
        make_docx(body=[("Heading 1", "Biogas"), ("Normal", "Biogas digesters work slowly in cold conditions.")],
                  with_table=False)
    job = pipe.process(pipe.submit(f"{item}.docx", data, lesson_manifest(item, **extra))[0]["job_id"])
    assert job["state"] == "succeeded", job
    return job["version_id"]


@pytest.mark.parametrize("kind", BACKENDS)
def test_blue_green_build_activate_rollback_retire(kind, gw, monkeypatch):
    monkeypatch.setattr(config, "INDEX_VALIDATION_SAMPLE", 10)

    async def body(store):
        pipe = Pipeline()
        mgr = IndexManager(store, pipe.registry)
        v1 = _ingest(pipe)
        await mgr.index_version(v1)
        first = await mgr.active()
        assert first.note == "bootstrap" and first.version == "nomic-embed-text"
        n1 = len(pipe.registry.chunks_of(v1))
        assert len(await store.chunk_ids(first.index_id)) == n1

        # The gateway now serves a new model under the same logical name.
        gw.version = "nomic-embed-text:v2"
        with pytest.raises(ModelChanged):
            await mgr.query("helicase", 3)
        new = await mgr.start_build(note="nomic v2")
        assert new.status == "building" and new.version == "nomic-embed-text:v2"
        with pytest.raises(IndexError_, match="already building"):
            await mgr.start_build()
        # A source approved during the build lands in the building index; the old
        # active index can no longer be written, so the job fails (retryable).
        v2 = _ingest(pipe, "cm9002", other=True)
        with pytest.raises(ModelChanged):
            await mgr.index_version(v2)
        assert {c["chunk_id"] for c in pipe.registry.chunks_of(v2)} <= await store.chunk_ids(new.index_id)
        built = await mgr.run_build(new.index_id)
        assert built.status == "ready", built.validation
        v = built.validation
        assert v["passed"] and v["missing"] == 0 and v["stale"] == 0 and v["self_recall_at_5"] >= 0.8
        assert built.stats["indexed_sources"] == 2

        active = await mgr.activate(new.index_id)
        assert active.status == "active" and active.previous_index_id == first.index_id
        rows, rec = await mgr.query("why do biogas digesters slow down in the cold", 3)
        assert rec.index_id == new.index_id and "Biogas" in rows[0]["text"]

        # Rolling back to an index whose model is no longer served is refused.
        with pytest.raises(IndexError_, match="cannot roll back"):
            await mgr.rollback()
        # Once the old model is served again, rollback catches the old index up first.
        gw.version = "nomic-embed-text"
        back = await mgr.rollback()
        assert back.index_id == first.index_id
        assert len(await store.chunk_ids(first.index_id)) == len(mgr.expected())
        with pytest.raises(IndexError_, match="active"):
            await mgr.retire(first.index_id)
        retired = await mgr.retire(new.index_id)
        assert retired.status == "retired"
        with pytest.raises(IndexError_, match="retired"):
            await mgr.activate(new.index_id)
    with_store(kind, body)


@pytest.mark.parametrize("kind", BACKENDS)
def test_validation_blocks_incomplete_index(kind, gw, monkeypatch):
    monkeypatch.setattr(config, "INDEX_VALIDATION_SAMPLE", 5)

    async def body(store):
        pipe = Pipeline()
        mgr = IndexManager(store, pipe.registry)
        await mgr.index_version(_ingest(pipe))
        new = await mgr.start_build()
        built = await mgr.run_build(new.index_id)
        victim = sorted(await store.chunk_ids(new.index_id))[0]
        await store.delete_chunks(new.index_id, [victim])
        with pytest.raises(IndexError_, match="1 missing"):
            await mgr.activate(new.index_id)
        assert (await store.get_index(new.index_id)).validation["missing_sample"] == [victim]
        forced = await mgr.activate(new.index_id, force=True)
        assert forced.status == "active" and built.status == "ready"
    with_store(kind, body)


@pytest.mark.parametrize("kind", BACKENDS)
def test_reprocess_and_supersede_keep_index_exact(kind, gw):
    async def body(store):
        pipe = Pipeline()
        mgr = IndexManager(store, pipe.registry)
        v1 = _ingest(pipe)
        await mgr.index_version(v1)
        rec = await mgr.active()
        old_ids = set(await store.chunk_ids(rec.index_id))
        # Reprocess the same bytes with a smaller chunk size: new chunk set replaces the old.
        m = lesson_manifest("cm9001")
        m["chunking"] = {"target_tokens": 96, "max_tokens": 128, "min_tokens": 20}
        job = pipe.process(pipe.submit("cm9001.docx", make_docx(extra_paragraphs=4, title="cm9001"), m)[0]["job_id"])
        await mgr.index_version(job["version_id"])
        now = await store.chunk_ids(rec.index_id)
        assert now == {c["chunk_id"] for c in pipe.registry.chunks_of(v1)} and now.isdisjoint(old_ids)
        # A newer approved version supersedes: only its vectors remain.
        v2 = _ingest(pipe, "cm9001", effective_from="2025-06-01", other=True)
        await mgr.index_version(v2)
        assert await store.chunk_ids(rec.index_id) == {c["chunk_id"] for c in pipe.registry.chunks_of(v2)}
        assert (await mgr.validate(rec.index_id))["passed"]
    with_store(kind, body)


def test_rebuild_of_unchanged_corpus_uses_cache(gw):
    async def body():
        pipe = Pipeline()
        mgr = IndexManager(InMemoryStore(), pipe.registry)
        await mgr.index_version(_ingest(pipe))
        before = sum(t.startswith("search_document: ") for t in gw.texts)
        new = await mgr.start_build(note="hnsw tuning")
        built = await mgr.run_build(new.index_id)
        after = sum(t.startswith("search_document: ") for t in gw.texts)
        assert built.status == "ready" and after == before  # no document re-embedded
        assert built.stats["cache_hits"] > 0
    run(body())


# ---------------------------------------------------------------- service endpoints

@pytest.fixture
def svc(gw, monkeypatch):
    pipe = Pipeline()
    monkeypatch.setattr(km, "PIPE", pipe)
    monkeypatch.setattr(km, "INDEX", IndexManager(InMemoryStore(), pipe.registry))
    with TestClient(km.app) as c:
        yield c


def _upload(c, token, auth, item="cm9001"):
    return c.post("/v1/knowledge/sources", headers=auth(token),
                  files={"file": (f"{item}.docx", make_docx(extra_paragraphs=3, title=item))},
                  data={"manifest": __import__("json").dumps(lesson_manifest(item))})


def test_index_endpoints_and_rbac(svc, gw, make_token, auth):
    c = svc
    teacher, eng = make_token(["teacher"]), make_token(["ai_engineer"])
    _upload(c, teacher, auth)
    h = c.get("/health").json()
    assert h["active_index"].startswith("idx_") and h["embed_fingerprint"].endswith("|nomic-embed")
    assert c.post("/v1/knowledge/indexes", headers=auth(teacher)).status_code == 403
    assert c.get("/v1/knowledge/indexes", headers=auth(teacher)).status_code == 403

    gw.version = "nomic-embed-text:v2"
    r = c.post("/v1/knowledge/query", headers=auth(make_token(["student"])), json={"query": "helicase"})
    assert r.status_code == 503 and "embedding model changed" in r.json()["detail"]
    detail = c.get(f"/v1/knowledge/indexes/{h['active_index']}?check_model=true", headers=auth(eng)).json()
    assert detail["model_matches"] is False

    built = c.post("/v1/knowledge/indexes", headers=auth(eng),
                   json={"activate_when_ready": True, "note": "model upgrade"})
    assert built.status_code == 202
    listing = c.get("/v1/knowledge/indexes", headers=auth(eng)).json()
    assert listing["active_index"] == built.json()["index_id"]
    r = c.post("/v1/knowledge/query", headers=auth(make_token(["student"])), json={"query": "helicase unwinds"})
    assert r.status_code == 200 and r.json()["x_nexa"]["index"] == built.json()["index_id"]
    v = c.post(f"/v1/knowledge/indexes/{built.json()['index_id']}/validate", headers=auth(eng)).json()
    assert v["passed"]
    assert c.delete(f"/v1/knowledge/indexes/{built.json()['index_id']}", headers=auth(eng)).status_code == 409
    assert c.post("/v1/knowledge/indexes/rollback", headers=auth(eng)).status_code == 409  # old model gone
    assert c.post("/v1/knowledge/indexes/idx_nope/activate", headers=auth(eng)).status_code == 404
    assert c.delete(f"/v1/knowledge/indexes/{h['active_index']}", headers=auth(eng)).json()["status"] == "retired"


def test_m1_raw_ingest_still_works(svc, make_token, auth):
    c = svc
    t = make_token(["teacher"])
    r = c.post("/v1/knowledge/ingest", headers=auth(t),
               json={"documents": [{"text": "Mount Wilhelm is the highest peak in Papua New Guinea."}]})
    assert r.json()["ingested"] == 1
    again = c.post("/v1/knowledge/ingest", headers=auth(t),
                   json={"documents": [{"text": "Mount Wilhelm is the highest peak in Papua New Guinea."}]})
    assert again.json()["ingested"] == 1
    res = c.post("/v1/knowledge/query", headers=auth(make_token(["student"])),
                 json={"query": "highest mountain in PNG", "top_k": 5}).json()["results"]
    assert len(res) == 1  # idempotent: the same raw text is stored once


def test_bow_vector_is_deterministic():
    assert bow_vector("Helicase unwinds DNA") == bow_vector("helicase UNWINDS dna")
