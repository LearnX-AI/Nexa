"""NEXA-M2-005: hybrid retrieval, filtering, reranking, authority, explain.

Filter correctness and fusion run against the in-memory store always and
against PostgreSQL + pgvector when NEXA_TEST_PG_DSN is set, in vector,
lexical and hybrid modes, and must give the same result sets.
"""
from __future__ import annotations

import asyncio
import itertools
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from v1.identity.principal import Principal
from v1.knowledge import config, embeddings
from v1.knowledge import main as km
from v1.knowledge.access import retrieval_filter
from v1.knowledge.authority import ClaimClass, arbitrate, classify_claim
from v1.knowledge.indexing import IndexManager
from v1.knowledge.ingestion.pipeline import Pipeline
from v1.knowledge.ingestion.registry import Registry
from v1.knowledge.retrieval import FeatureReranker, RetrievalRequest, Retriever, query_coverage
from v1.knowledge.store import InMemoryStore, IndexRecord

from .knowledge_fixtures import FakeGateway, bow_vector, lesson_manifest, make_docx

PG_DSN = os.environ.get("NEXA_TEST_PG_DSN", "")
BACKENDS = ["memory"] + (["pgvector"] if PG_DSN else [])
DIM = 128


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def gw(monkeypatch):
    g = FakeGateway()
    monkeypatch.setattr(embeddings, "_gateway", g)
    return g


async def _store(kind):
    if kind == "memory":
        return InMemoryStore()
    from v1.knowledge.pgstore import PgVectorStore
    s = PgVectorStore(PG_DSN, schema="r_" + uuid.uuid4().hex[:10])
    await s.open()
    return s


async def _drop(s):
    if s.name == "pgvector":
        async with s._conn() as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS {s.schema} CASCADE")
        await s.close()


def md(**kw):
    base = {"approval_status": "approved", "access_classification": "learner", "scope": "national",
            "effective_from": "2024-01-01", "subject": "Biology", "grade_levels": "12", "language": "en",
            "source_type": "official_syllabus", "authority_tier": 1, "title": "Biology Syllabus Grade 12",
            "source_id": "png-ndoe:bio-syllabus"}
    base.update(kw)
    return base


# A small corpus that exercises every filter dimension.
CORPUS = [
    ("syl-photo", "Photosynthesis converts light energy into chemical energy in chloroplasts.",
     md(module_code="12.1", topic_code="12.1.2", outcome_codes="12.1.2a")),
    ("lms-photo", "Lesson notes: photosynthesis happens in chloroplasts using light energy and water.",
     md(source_type="lms_content", authority_tier=4, title="Photosynthesis lesson", source_id="lms:s1:cm1",
        module_code="12.1", topic_code="12.1.2")),
    ("chem-bond", "Covalent bonds form when atoms share electron pairs.",
     md(subject="Chemistry", grade_levels="11", title="Chemistry Syllabus", source_id="png-ndoe:chem")),
    ("tpi-note", "Fotosintesis em i wok bilong lip long kisim san.",
     md(language="tpi", source_type="lms_content", authority_tier=4, source_id="lms:s1:cm2", title="Tok Pisin")),
    ("teacher-key", "Answer key: photosynthesis question 3 is chloroplast.",
     md(access_classification="teacher_only", source_type="assessment", authority_tier=2, title="Answer key",
        source_id="png-ndoe:key")),
    ("school-a", "Our school garden shows photosynthesis in sweet potato leaves.",
     md(scope="school", school_id="school-a", source_type="teacher_material", authority_tier=5,
        title="Garden notes", source_id="lms:a:cm3")),
    ("school-b", "School B notes on photosynthesis in taro leaves.",
     md(scope="school", school_id="school-b", source_type="teacher_material", authority_tier=5,
        title="Taro notes", source_id="lms:b:cm4")),
    ("pending", "Draft notes on photosynthesis.", md(approval_status="pending_review", source_id="d:1")),
    ("future", "Future syllabus photosynthesis outcome.", md(effective_from="2099-01-01", source_id="f:1")),
    ("expired", "Old photosynthesis notes.", md(effective_to="2020-01-01", source_id="e:1")),
    ("gr11-bio", "Cells contain organelles such as chloroplasts and mitochondria.",
     md(grade_levels="11", module_code="11.2", source_id="png-ndoe:bio11", title="Biology Grade 11")),
    ("raw", "Raw M1 note about photosynthesis.", {}),
]


async def _load(store):
    rec = await store.create_index(IndexRecord("idx_r1", "nexa-embed-default", "nomic-embed-text", "ollama", DIM,
                                               "nomic-embed"))
    await store.set_active(rec.index_id)
    await store.upsert("idx_r1", [{"chunk_id": cid, "version_id": "v-" + cid, "text": text,
                                   "vector": bow_vector("search_document: " + text, DIM),
                                   "metadata": {**m, "chunk_id": cid}} for cid, text, m in CORPUS])


def _who(*roles, school=None):
    return retrieval_filter(Principal("u", tuple(roles), "user", school_id=school))


def _expected(where, query_terms=None):
    return {cid for cid, _, m in CORPUS if where({**m, "chunk_id": cid})}


FILTER_CASES = [
    ({}, None),
    ({"subjects": ["Biology"]}, None),
    ({"subjects": ["Chemistry"], "grades": [11]}, None),
    ({"grades": [12]}, None),
    ({"languages": ["tpi"]}, None),
    ({"source_types": ["lms_content"]}, None),
    ({"max_tier": 1}, None),
    ({"module_codes": ["12.1"]}, None),
    ({"topic_codes": ["12.1.2"]}, None),
    ({"scopes": ["school"]}, None),
]
PRINCIPALS = [("student",), ("teacher",), ("platform_admin",)]


@pytest.mark.parametrize("kind", BACKENDS)
def test_filtering_correctness_all_modes_and_stores(kind, gw):
    """Every filter, alone and per role, returns exactly the passages the
    Python rules allow, in every mode, on both stores."""
    async def go():
        s = await _store(kind)
        try:
            await _load(s)
            mgr = IndexManager(s, Registry())
            r = Retriever(mgr)
            for (flt, _), roles, school in itertools.product(FILTER_CASES, PRINCIPALS, (None, "school-a")):
                where = _who(*roles, school=school)
                want = _expected(where.narrow(**flt))
                for mode in ("vector", "lexical", "hybrid"):
                    res = await r.retrieve(RetrievalRequest("photosynthesis chloroplasts light energy cells "
                                                            "covalent bonds fotosintesis notes", top_k=50,
                                                            filters=flt, mode=mode), where)
                    got = {x["chunk_id"] for x in res.results}
                    # Lexical returns only passages sharing a word with the query;
                    # it must never return one the filter excludes.
                    assert got <= want, (kind, mode, flt, roles, school, got - want)
                    if mode != "lexical":
                        assert got == want, (kind, mode, flt, roles, school, want - got)
        finally:
            await _drop(s)
    run(go())


def test_filter_rules_access_and_scope():
    student = _who("student")
    allowed = _expected(student)
    assert "teacher-key" not in allowed and "pending" not in allowed and "future" not in allowed
    assert "expired" not in allowed and "school-a" not in allowed and "school-b" not in allowed
    assert "raw" in allowed  # M1 raw documents pass access...
    assert "raw" not in _expected(student.narrow(subjects=["Biology"]))  # ...but never match a filter
    a = _expected(_who("student", school="school-a"))
    assert "school-a" in a and "school-b" not in a
    assert {"school-a", "school-b"} <= _expected(_who("platform_admin"))
    assert "teacher-key" in _expected(_who("teacher")) and "teacher-key" not in _expected(_who("student"))
    # Filters never widen access.
    assert "teacher-key" not in _expected(student.narrow(source_types=["assessment"]))
    with pytest.raises(ValueError):
        student.narrow(nonsense=["x"])


def test_school_scope_sql_matches_python():
    f = _who("student", school="school-a")
    clause, params = f.sql()
    assert "school_id = %s" in clause and params[-1] == "school-a"
    clause, params = _who("platform_admin").sql()
    assert "school_id" not in clause


@pytest.mark.parametrize("kind", BACKENDS)
def test_hybrid_fuses_vector_and_lexical(kind, monkeypatch):
    """A passage only the vector retriever finds and one only the lexical
    retriever finds both reach the hybrid results; one both find leads."""
    a, b = [1.0] + [0.0] * (DIM - 1), [0.0, 1.0] + [0.0] * (DIM - 2)

    async def gateway(texts, model, trace_id=None):
        return [a if "query" in t else b for t in texts], {"model_version": "nomic-embed-text", "runtime": "ollama"}

    monkeypatch.setattr(embeddings, "_gateway", gateway)

    async def go():
        s = await _store(kind)
        try:
            await s.create_index(IndexRecord("idx_f1", "nexa-embed-default", "nomic-embed-text", "ollama", DIM,
                                             "nomic-embed"))
            await s.set_active("idx_f1")
            rows = [("both", "mitochondria release energy", a), ("vec_only", "the powerhouse of the cell", a),
                    ("lex_only", "mitochondria are organelles", b), ("neither", "rainfall in Lae", b)]
            await s.upsert("idx_f1", [{"chunk_id": c, "version_id": "v", "text": t, "vector": v, "metadata": {}}
                                      for c, t, v in rows])
            r = Retriever(IndexManager(s, Registry()))
            where = _who("student")
            hy = await r.retrieve(RetrievalRequest("mitochondria energy", 3, mode="hybrid"), where)
            ids = [x["chunk_id"] for x in hy.results]
            assert ids[0] == "both" and {"vec_only", "lex_only"} <= set(ids)
            top = hy.results[0]["scores"]
            assert top["vector_rank"] and top["lexical_rank"] and top["rrf"] > hy.results[1]["scores"]["rrf"]
            lex_only = next(x for x in hy.results if x["chunk_id"] == "lex_only")
            assert lex_only["scores"]["similarity"] == pytest.approx(0.0, abs=1e-6)  # cosine fetched for it
            vec = await r.retrieve(RetrievalRequest("mitochondria energy", 2, mode="vector"), where)
            assert {x["chunk_id"] for x in vec.results} == {"both", "vec_only"}
            lex = await r.retrieve(RetrievalRequest("mitochondria energy", 3, mode="lexical"), where)
            assert "vec_only" not in {x["chunk_id"] for x in lex.results}
            assert hy.explain["candidates"]["vector"] == 4 and hy.explain["candidates"]["lexical"] == 2
        finally:
            await _drop(s)
    run(go())


def test_explain_payload_and_reasons(gw):
    async def go():
        s = InMemoryStore()
        await _load(s)
        r = Retriever(IndexManager(s, Registry()))
        res = await r.retrieve(RetrievalRequest("photosynthesis chloroplasts", 3, filters={"grades": [12]},
                                                rerank=True, profile="edge"), _who("student"))
        ex = res.explain
        assert ex["mode"] == "hybrid" and ex["profile"] == "edge" and ex["reranker"] == "features-1.0"
        assert ex["filters"]["grades"] == [12] and ex["filters"]["access_classes"] == ["learner", "public"]
        assert set(ex["timings_ms"]) >= {"embed", "vector", "lexical", "fuse", "rerank", "authority", "total"}
        assert ex["candidates"]["pool"] <= config.PROFILES["edge"]["rerank_pool"]
        for i, x in enumerate(res.results, 1):
            assert x["rank"] == i and x["reasons"][0].startswith("found by")
            assert {"rrf", "similarity", "rerank", "fused_rank", "rerank_rank"} <= set(x["scores"])
            assert x["score"] == x["scores"]["similarity"]
            assert set(x["coverage"]) == {"coverage", "phrase", "heading"}
            assert "authority" in x
    run(go())


def test_reranker_prefers_covering_passage():
    items = [
        {"chunk_id": "a", "text": "Rainfall patterns in the highlands vary by season.", "metadata": {},
         "scores": {"rrf": 0.033, "similarity": 0.52}},
        {"chunk_id": "b", "text": "Helicase unwinds the DNA double helix at the replication fork.",
         "metadata": {"title": "DNA replication"}, "scores": {"rrf": 0.032, "similarity": 0.50}},
    ]
    out = FeatureReranker().score("which enzyme unwinds the DNA double helix", items)
    assert out[0]["chunk_id"] == "b" and out[0]["scores"]["rerank"] > out[1]["scores"]["rerank"]
    cov = query_coverage("which enzyme unwinds the DNA double helix", items[1])
    assert cov["coverage"] > 0.6 and cov["phrase"] > 0 and cov["heading"] > 0


# ---------------------------------------------------------------- authority

def _r(cid, tier, rank_title=None, **m):
    st = {1: "official_syllabus", 2: "teacher_guide", 3: "student_guide", 4: "lms_content",
          5: "teacher_material", 6: "external_reference"}[tier]
    return {"chunk_id": cid, "text": cid, "metadata": {"authority_tier": tier, "source_type": st,
                                                      "source_id": f"s:{cid}", "title": rank_title or cid,
                                                      "scope": m.pop("scope", "national"), **m},
            "scores": {}, "reasons": []}


def test_authority_official_not_silently_overridden_by_lms():
    lms = _r("lms", 4, topic_code="12.1.2")
    syl = _r("syl", 1, topic_code="12.1.2")
    other = _r("other", 4, topic_code="12.5.1")
    out, conflicts = arbitrate([lms, other, syl], ClaimClass.INSTRUCTIONAL)
    ids = [x["chunk_id"] for x in out]
    assert ids.index("syl") < ids.index("lms")  # official leads where both apply
    assert "lms" in ids  # LMS still supplements
    assert ids.index("other") > ids.index("lms") or ids == ["syl", "lms", "other"]
    lms_out = next(x for x in out if x["chunk_id"] == "lms")
    assert lms_out["authority"]["authoritative"] is False and "R4" in lms_out["authority"]["notes"][0]
    assert conflicts == [{"rule": "R4", "point": "topic:12.1.2", "authoritative": "syl", "authoritative_tier": 1,
                          "lower": "lms", "lower_tier": 4}]


def test_authority_shared_outcome_and_scope():
    school = _r("school", 5, outcome_codes="12.2.1a,12.2.1b", scope="school")
    lms = _r("lms", 4, outcome_codes="12.2.1b")
    out, c = arbitrate([school, lms])
    assert [x["chunk_id"] for x in out] == ["lms", "school"] and c[0]["point"] == "outcome:12.2.1b"
    # R5: same tier, national outranks school scope.
    loc = _r("loc", 4, topic_code="11.1.1", scope="school")
    nat = _r("nat", 4, topic_code="11.1.1")
    out, _ = arbitrate([loc, nat])
    assert [x["chunk_id"] for x in out] == ["nat", "loc"]


def test_authority_leaves_unrelated_passages_in_relevance_order():
    a, b, c = _r("a", 5, topic_code="1.1.1"), _r("b", 1, topic_code="2.2.2"), _r("c", 4)
    out, conflicts = arbitrate([a, b, c])
    assert [x["chunk_id"] for x in out] == ["a", "b", "c"] and conflicts == []


def test_authority_outcome_questions_and_external():
    assert classify_claim("What are the learning outcomes for 12.1.2?") == ClaimClass.CURRICULUM_OUTCOME
    assert classify_claim("Explain photosynthesis") == ClaimClass.INSTRUCTIONAL
    lms, syl = _r("lms", 4), _r("syl", 1)
    out, c = arbitrate([lms, syl], ClaimClass.CURRICULUM_OUTCOME)
    assert [x["chunk_id"] for x in out] == ["syl", "lms"] and c[0]["rule"] == "R3"
    assert out[1]["authority"]["authoritative"] is False
    ext, guide = _r("wiki", 6), _r("guide", 3)
    out, _ = arbitrate([ext, guide])
    assert [x["chunk_id"] for x in out] == ["guide", "wiki"] and out[1]["authority"]["external"]
    only_ext, _ = arbitrate([_r("wiki", 6)])
    assert only_ext[0]["authority"]["authoritative"] is True  # R7: used when nothing else covers it


def test_authority_conflict_through_retriever(gw):
    """An LMS passage that is the closer match still ranks below the official
    passage on the same topic, and the response says why."""
    async def go():
        s = InMemoryStore()
        await _load(s)
        r = Retriever(IndexManager(s, Registry()))
        res = await r.retrieve(RetrievalRequest("Lesson notes photosynthesis chloroplasts light energy water", 5),
                               _who("student"))
        ids = [x["chunk_id"] for x in res.results]
        assert ids.index("syl-photo") < ids.index("lms-photo")
        assert any(c["authoritative"] == "syl-photo" and c["lower"] == "lms-photo" for c in res.explain["conflicts"])
        moved = next(x for x in res.results if x["chunk_id"] == "syl-photo")
        assert any("authority rules moved it" in why for why in moved["reasons"]) or ids[0] == "syl-photo"
    run(go())


# ---------------------------------------------------------------- API

@pytest.fixture
def svc(monkeypatch):
    g = FakeGateway()
    monkeypatch.setattr(embeddings, "_gateway", g)
    pipe = Pipeline()
    monkeypatch.setattr(km, "PIPE", pipe)
    monkeypatch.setattr(km, "INDEX", IndexManager(InMemoryStore(), pipe.registry))
    with TestClient(km.app) as c:
        yield c


def test_query_api_filters_explain_and_validation(svc, make_token, auth):
    import json as _json
    c = svc
    teacher = make_token(["teacher"])
    up = c.post("/v1/knowledge/sources", headers=auth(teacher), files={"file": ("l.docx", make_docx())},
                data={"manifest": _json.dumps(lesson_manifest())})
    assert up.status_code == 202
    st = auth(make_token(["student"]))
    body = {"query": "which enzyme unwinds DNA", "top_k": 3, "explain": True,
            "filters": {"subjects": ["Biology"], "grades": [12]}}
    r = c.post("/v1/knowledge/query", headers=st, json=body).json()
    assert r["results"] and r["retrieval"]["filters"]["subjects"] == ["Biology"]
    top = r["results"][0]
    assert top["chunk_id"] and top["rank"] == 1 and top["provenance"]["version_id"] and top["scores"]["rrf"]
    none = c.post("/v1/knowledge/query", headers=st, json={**body, "filters": {"subjects": ["Physics"]}}).json()
    assert none["results"] == []
    assert c.post("/v1/knowledge/query", headers=st, json={**body, "filters": {"colour": ["red"]}}).status_code == 422
    assert c.post("/v1/knowledge/query", headers=st, json={**body, "mode": "fuzzy"}).status_code == 422
    plain = c.post("/v1/knowledge/query", headers=st, json={"query": "helicase"}).json()
    assert plain["retrieval"] is None and plain["x_nexa"]["mode"] == "hybrid"
    lex = c.post("/v1/knowledge/query", headers=st, json={"query": "helicase", "mode": "lexical"}).json()
    assert lex["x_nexa"]["mode"] == "lexical" and "Helicase" in lex["results"][0]["text"]


def test_lexical_unavailable_degrades_to_vector(gw):
    async def go():
        s = InMemoryStore()
        await _load(s)
        s.lexical_available = lambda index_id: False
        res = await Retriever(IndexManager(s, Registry())).retrieve(RetrievalRequest("photosynthesis", 3),
                                                                     _who("student"))
        assert res.explain["mode"] == "vector" and "degraded" in res.explain and res.results
    run(go())


@pytest.mark.skipif(not PG_DSN, reason="NEXA_TEST_PG_DSN not set")
def test_pgvector_adds_m2005_columns_to_existing_tables():
    async def go():
        from v1.knowledge.pgstore import PgVectorStore
        s = PgVectorStore(PG_DSN, schema="m_" + uuid.uuid4().hex[:10])
        await s.open()
        try:
            async with s._conn() as conn:
                await conn.execute(f"INSERT INTO {s.schema}.index_versions (index_id, status, record) VALUES "
                                   f"('idx_old1', 'active', %s)", ('{"index_id": "idx_old1", "logical": "m", '
                                                                   '"version": "v", "runtime": "r", "dim": 3, '
                                                                   '"prefix_scheme": "none", "status": "active"}',))
                await conn.execute(f"CREATE TABLE {s.schema}.vec_idx_old1 (chunk_id text PRIMARY KEY, version_id "
                                   "text NOT NULL, text text NOT NULL, metadata jsonb NOT NULL, approval_status "
                                   "text, access_classification text, scope text, effective_from date, "
                                   "effective_to date, embedding vector(3) NOT NULL)")
                await conn.execute(f"INSERT INTO {s.schema}.vec_idx_old1 VALUES ('o1', 'v', 'Mitochondria release "
                                   "energy', %s, 'approved', 'learner', 'national', NULL, NULL, '[1,0,0]')",
                                   ('{"subject": "Biology", "grade_levels": "12", "authority_tier": 1}',))
            await s.close()
            await s.open()  # migration runs on open
            assert s.lexical_available("idx_old1")
            f = _who("student").narrow(subjects=["Biology"], grades=[12], max_tier=1)
            assert [r["chunk_id"] for r in await s.lexical_query("idx_old1", "mitochondria", 5, where=f)] == ["o1"]
            assert [r["chunk_id"] for r in await s.query("idx_old1", [1, 0, 0], 5, where=f)] == ["o1"]
        finally:
            async with s._conn() as conn:
                await conn.execute(f"DROP SCHEMA IF EXISTS {s.schema} CASCADE")
            await s.close()
    run(go())
