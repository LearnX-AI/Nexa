"""NEXA-M2-009 school edge: packages, offline retrieval, sync, reconnect,
tamper resistance, data minimisation."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from v1.edge import package as pkg
from v1.edge import sync, web as edge_web
from v1.edge.edgestore import EdgeStore, current_version
from v1.identity import tokens
from v1.knowledge import config, embeddings, grounding
from v1.knowledge import main as km
from v1.knowledge.access import retrieval_filter
from v1.knowledge.indexing import IndexManager
from v1.knowledge.ingestion.pipeline import Pipeline
from v1.knowledge.retrieval import RetrievalRequest, Retriever
from v1.knowledge.store import IndexRecord, InMemoryStore
from v1.identity.principal import Principal

from .knowledge_fixtures import FakeGateway, bow_vector

DIM = 128
MASTER = bytes(range(32))
KEY = pkg.school_key(MASTER, "pomnsoe")
INDEX_INFO = {"index_id": "idx_core", "fingerprint": f"nexa-embed-default|nomic-embed-text|ollama|{DIM}|nomic-embed",
              "logical": "nexa-embed-default", "version": "nomic-embed-text", "runtime": "ollama", "dim": DIM,
              "prefix_scheme": "nomic-embed"}
BASE = {"approval_status": "approved", "access_classification": "learner", "language": "en", "scope": "national",
        "effective_from": "2022-01-01"}


def _doc(vid, texts, **md):
    out = []
    for i, t in enumerate(texts):
        cid = f"{vid}:{i}"
        m = {**BASE, "version_id": vid, "source_id": vid.split("@")[0], "chunk_id": cid, "ordinal": i,
             "title": md.get("title", vid), "citation": f"{vid} p.{i + 1}", "subject": "Biology",
             "grade_levels": "12", "source_type": "student_guide", **md}
        out.append({"chunk_id": cid, "version_id": vid, "text": t, "metadata": m,
                    "vector": bow_vector("search_document: " + t, DIM)})
    return out


def corpus():
    return (
        _doc("bio-sg@v1", ["DNA is copied in the S (Synthesis) phase of the cell cycle.",
                           "Helicase unwinds the DNA double helix by breaking hydrogen bonds."],
             title="STEM Biology Student Guide Grade 12")
        + _doc("phy-syl@v1", ["12.1.1c Clearly explain how electromagnets and transformers use electromagnetic "
                              "induction."], subject="Physics", source_type="official_syllabus", authority_tier=1,
               title="STEM Physics Syllabus Grade 12")
        + _doc("pomnsoe-garden@v1", ["Our Pomnsoe school garden grows kaukau and taro in raised beds."],
               scope="school", school_id="pomnsoe", source_type="lms_content", title="Pomnsoe garden notes")
        + _doc("hutjena-reef@v1", ["Hutjena students survey the reef at Buka passage."],
               scope="school", school_id="hutjena", source_type="lms_content", title="Hutjena reef notes")
        + _doc("bio-tg@v1", ["Teacher note: 79 families and 730 species of birds in PNG."],
               access_classification="teacher_only", source_type="teacher_guide", title="Biology Teacher Guide")
        + _doc("bio-key@v1", ["Answer key: question 3 is (b)."], access_classification="restricted",
               source_type="assessment", title="Biology answer key")
        + _doc("draft@v1", ["Draft lesson not yet approved about mitochondria."], approval_status="draft")
    )


async def _core_store(items):
    s = InMemoryStore()
    await s.create_index(IndexRecord("idx_core", "nexa-embed-default", "nomic-embed-text", "ollama", DIM,
                                     "nomic-embed"))
    await s.set_active("idx_core")
    await s.upsert("idx_core", items)
    return s


def _build(tmp, items, version, previous=None, policy=None):
    pol = policy or pkg.Policy(school_id="pomnsoe")
    s = asyncio.run(_core_store(items))
    sel, skipped = asyncio.run(pkg.collect(s, "idx_core", pol))
    probes = [bow_vector(t, DIM) for t in pkg.PROBE_TEXTS]
    return pkg.build(sel, policy=pol, index=INDEX_INFO, version=version, out_root=str(tmp / "core"), key=KEY,
                     probes=probes, skipped=skipped, previous=previous)


async def _probe(texts):
    return [bow_vector(t, DIM) for t in texts]


@pytest.fixture
def home(tmp_path):
    return str(tmp_path / "edge")


# ---------------------------------------------------------------- package contents (data minimisation)

def test_package_holds_only_what_this_school_may_have(tmp_path):
    full = _build(tmp_path, corpus(), 1)[0]
    m = full.manifest
    titles = {s["title"] for s in m["sources"].values()}
    assert titles == {"STEM Biology Student Guide Grade 12", "STEM Physics Syllabus Grade 12",
                      "Pomnsoe garden notes", "Biology Teacher Guide"}
    assert m["stats"]["excluded"] == {"another school's content": 1, "restricted (assessment material stays on core)": 1,
                                      "not approved": 1}
    assert m["contains_learner_data"] is False and m["school_id"] == "pomnsoe"
    con = sqlite3.connect(os.path.join(full.directory, "knowledge.sqlite"))
    cols = {r[1] for r in con.execute("PRAGMA table_info(chunks)")}
    assert cols == {"chunk_id", "version_id", "text", "metadata", "vector", "embedding_text"}   # curriculum only
    no_teacher = _build(tmp_path / "b", corpus(), 1, policy=pkg.Policy(school_id="pomnsoe",
                                                                       include_teacher_only=False))[0]
    assert "Biology Teacher Guide" not in {s["title"] for s in no_teacher.manifest["sources"].values()}
    physics_only = _build(tmp_path / "c", corpus(), 1, policy=pkg.Policy(school_id="pomnsoe",
                                                                         subjects=["Physics"]))[0]
    assert {s["subject"] for s in physics_only.manifest["sources"].values()} == {"Physics"}


# ---------------------------------------------------------------- offline retrieval

def test_offline_retrieval_matches_core(tmp_path, home):
    items = corpus()
    full = _build(tmp_path, items, 1)[0]
    asyncio.run(sync.install_bundle(home, full.bundle, KEY, "pomnsoe", _probe))
    edge = EdgeStore(home)

    async def run():
        core = await _core_store(items)
        gw = FakeGateway(dim=DIM)
        embeddings._gateway, saved = gw, embeddings._gateway
        try:
            out = {}
            for name, st in (("core", core), ("edge", edge)):
                r = Retriever(IndexManager(st, Pipeline().registry))
                who = retrieval_filter(Principal("s", ("student",), "user", school_id="pomnsoe"))
                for q in ("During which phase of the cell cycle is DNA copied?",
                          "Which indicator covers electromagnets and transformers?",
                          "What grows in the school garden?"):
                    rr = await r.retrieve(RetrievalRequest(q, 3, mode="vector"), who)
                    out.setdefault(q, {})[name] = [x["chunk_id"] for x in rr.results]
            return out
        finally:
            embeddings._gateway = saved
    for q, by in asyncio.run(run()).items():
        assert by["edge"] == by["core"], q          # same vectors, same ranking, offline
    assert asyncio.run(edge.lexical_query("edge_pomnsoe_v1", "kaukau taro garden", 3))[0]["chunk_id"] == \
        "pomnsoe-garden@v1:0"


# ---------------------------------------------------------------- versioning, delta, reconnect

def test_delta_update_applies_changes_and_withdrawals(tmp_path, home):
    v1 = _build(tmp_path, corpus(), 1)[0]
    asyncio.run(sync.install_bundle(home, v1.bundle, KEY, "pomnsoe", _probe))
    items = [it for it in corpus() if not it["version_id"].startswith("pomnsoe-garden")]   # withdrawn on core
    for it in items:
        if it["chunk_id"] == "bio-sg@v1:0":
            it["text"] = "DNA replication happens in the S (Synthesis) phase, before mitosis."
            it["vector"] = bow_vector("search_document: " + it["text"], DIM)
    items += _doc("chem-sg@v1", ["A covalent bond forms when two atoms share a pair of electrons."],
                  subject="Chemistry")
    res = _build(tmp_path, items, 2, previous=v1.manifest)
    delta = next(r for r in res if r.kind == "delta")
    assert delta.manifest["delta"]["removed"] == ["pomnsoe-garden@v1"]
    assert set(delta.manifest["delta"]["changed"]) == {"bio-sg@v1", "chem-sg@v1"}
    n = sqlite3.connect(os.path.join(delta.directory, "delta.sqlite")).execute("SELECT count(*) FROM chunks").fetchone()
    assert n[0] == 3 < delta.manifest["stats"]["chunks"]          # only the changed sources travel
    got = asyncio.run(sync.install_bundle(home, delta.bundle, KEY, "pomnsoe", _probe))
    assert (got.version, got.kind, got.from_version) == (2, "delta", 1) and current_version(home) == 2
    st = EdgeStore(home)
    texts = asyncio.run(st.all_texts("edge_pomnsoe_v2"))
    assert any("before mitosis" in t for t in texts) and not any("kaukau" in t for t in texts)
    assert any("covalent bond" in t for t in texts)
    assert asyncio.run(st.lexical_query("edge_pomnsoe_v2", "kaukau garden", 3)) == []   # FTS updated too
    assert sync.rollback(home) == 1 and current_version(home) == 1


def test_pull_from_core_up_to_date_update_and_offline(tmp_path, home, monkeypatch):
    out = tmp_path / "core"
    v1 = _build(tmp_path, corpus(), 1)[0]
    monkeypatch.setattr(config, "EDGE_PACKAGE_DIR", str(out))
    tok = tokens.issue_token("edge:pomnsoe", ["edge_node"], "service", 600, extra={"school_id": "pomnsoe"})
    other = tokens.issue_token("edge:hutjena", ["edge_node"], "service", 600, extra={"school_id": "hutjena"})
    transport = httpx.ASGITransport(app=km.app)

    async def go(token):
        async with httpx.AsyncClient(transport=transport, base_url="http://core") as c:
            return await sync.pull(home, "http://core", token, KEY, "pomnsoe", _probe, client=c)
    with pytest.raises(sync.SyncError, match="403"):
        asyncio.run(go(other))                       # a school server cannot fetch another school's package
    assert asyncio.run(go(tok)).version == 1
    assert asyncio.run(go(tok)) is None              # up to date
    _build(tmp_path, corpus() + _doc("chem@v1", ["Ionic bonds transfer electrons."], subject="Chemistry"), 2,
           previous=v1.manifest)
    r = asyncio.run(go(tok))
    assert (r.version, r.kind) == (2, "delta")       # reconnect: only the change travels

    async def offline():
        def fail(req):
            raise httpx.ConnectError("no route to core")
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as c:
            return await sync.pull(home, "http://core", tok, KEY, "pomnsoe", _probe, client=c)
    with pytest.raises(sync.SyncError, match="not reachable"):
        asyncio.run(offline())
    assert current_version(home) == 2                # still serving what it has
    assert any(h["event"] == "offline" for h in sync.history(home))


def test_a_school_offline_for_several_releases_gets_a_full_package(tmp_path, home):
    v1 = _build(tmp_path, corpus(), 1)[0]
    asyncio.run(sync.install_bundle(home, v1.bundle, KEY, "pomnsoe", _probe))
    v2 = _build(tmp_path, corpus()[:4], 2, previous=v1.manifest)
    v3 = _build(tmp_path, corpus()[:3], 3, previous=v2[0].manifest)
    delta3 = next(r for r in v3 if r.kind == "delta")
    with pytest.raises(sync.SyncError, match="full package is needed"):
        asyncio.run(sync.install_bundle(home, delta3.bundle, KEY, "pomnsoe", _probe))
    assert asyncio.run(sync.install_bundle(home, v3[0].bundle, KEY, "pomnsoe", _probe)).version == 3


# ---------------------------------------------------------------- tamper and safety checks

def _unpacked(tmp_path, bundle):
    d = tmp_path / "unpacked"
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    sync.unpack(bundle, str(d))
    return d


def test_tampered_wrong_school_old_or_wrong_model_packages_are_refused(tmp_path, home):
    full = _build(tmp_path, corpus(), 1)[0]
    d = _unpacked(tmp_path, full.bundle)
    m = json.loads((d / "manifest.json").read_text())
    m["sources"]["bio-sg@v1"]["sha256"] = "0" * 64
    (d / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(sync.SyncError, match="signature"):
        asyncio.run(sync.install(home, str(d), KEY, "pomnsoe", _probe))
    d = _unpacked(tmp_path, full.bundle)
    with open(d / "knowledge.sqlite", "ab") as fh:
        fh.write(b"x")
    with pytest.raises(sync.SyncError, match="does not match the manifest"):
        asyncio.run(sync.install(home, str(d), KEY, "pomnsoe", _probe))
    with pytest.raises(sync.SyncError, match="signature"):
        asyncio.run(sync.install_bundle(home, full.bundle, pkg.school_key(MASTER, "hutjena"), "hutjena", _probe))
    with pytest.raises(sync.SyncError, match="is for school"):
        asyncio.run(sync.install_bundle(home, full.bundle, KEY, "hutjena", _probe))

    async def other_model(texts):
        return [bow_vector("different " + t, DIM) for t in texts]
    with pytest.raises(sync.SyncError, match="embedding model"):
        asyncio.run(sync.install_bundle(home, full.bundle, KEY, "pomnsoe", other_model))
    assert current_version(home) is None             # nothing was ever served
    asyncio.run(sync.install_bundle(home, full.bundle, KEY, "pomnsoe", _probe))
    with pytest.raises(sync.SyncError, match="not newer"):
        asyncio.run(sync.install_bundle(home, full.bundle, KEY, "pomnsoe", _probe))


# ---------------------------------------------------------------- the school server as a service

@pytest.fixture
def school_node(tmp_path, home, monkeypatch):
    full = _build(tmp_path, corpus(), 1)[0]
    asyncio.run(sync.install_bundle(home, full.bundle, KEY, "pomnsoe", _probe))
    st = EdgeStore(home)
    monkeypatch.setattr(km, "INDEX", IndexManager(st, Pipeline().registry))
    monkeypatch.setattr(embeddings, "_gateway", FakeGateway(dim=DIM))
    monkeypatch.setattr(config, "EDGE_MODE", True)
    monkeypatch.setattr(config, "EDGE_HOME", home)
    monkeypatch.setattr(config, "EDGE_SCHOOL_ID", "pomnsoe")
    monkeypatch.setattr(config, "EDGE_CORE_URL", "http://core.invalid")
    monkeypatch.setattr(config, "EDGE_CORE_TOKEN", "t")
    monkeypatch.setattr(config, "EDGE_ANSWER_ROUTE", "core_first")
    monkeypatch.setattr(config, "EDGE_CORE_TIMEOUT_S", 0.5)
    monkeypatch.setattr(config, "EVIDENCE_THRESHOLDS", {"default": {"min": 0.2, "strong": 0.3}})
    monkeypatch.setattr(edge_web, "CACHE", edge_web.AnswerCache(100, 3600))
    calls = []

    async def chat(messages, trace_id=None):
        calls.append(messages)
        return "DNA is copied in the S (Synthesis) phase [S1].", "nexa-chat-precise"
    monkeypatch.setattr(grounding, "chat", chat)
    with TestClient(km.app) as c:
        yield c, calls


def _h(roles, school=None):
    extra = {"school_id": school} if school else None
    return {"Authorization": "Bearer " + tokens.issue_token("user:s1", roles, "user", 300, extra=extra)}


def test_school_node_answers_offline_and_caches_repeated_questions(school_node):
    c, calls = school_node
    q = {"question": "During which phase of the cell cycle is DNA copied?"}
    a = c.post("/v1/knowledge/answer", headers=_h(["student"]), json=q).json()
    assert a["status"] == "grounded" and a["x_nexa"]["route"] == "local"   # core unreachable: answered locally
    assert a["x_nexa"]["package_version"] == 1 and len(calls) == 1
    b = c.post("/v1/knowledge/answer", headers=_h(["student"]), json={"question": "during which PHASE of the "
                                                                                  "cell cycle is DNA copied"}).json()
    assert b["x_nexa"]["route"] == "cache" and len(calls) == 1 and b["answer"] == a["answer"]
    # users of this school node see this school's content even without a school claim in their token
    r = c.post("/v1/knowledge/query", headers=_h(["student"]), json={"query": "school garden kaukau taro",
                                                                      "top_k": 3}).json()
    assert r["results"][0]["chunk_id"] == "pomnsoe-garden@v1:0"
    # teacher-only stays teacher-only on the school node
    r = c.post("/v1/knowledge/query", headers=_h(["student"]), json={"query": "families species birds",
                                                                      "top_k": 5}).json()
    assert all(x["metadata"]["access_classification"] != "teacher_only" for x in r["results"])
    r = c.post("/v1/knowledge/query", headers=_h(["teacher"]), json={"query": "families species birds",
                                                                      "top_k": 5}).json()
    assert any(x["metadata"]["access_classification"] == "teacher_only" for x in r["results"])
    s = c.get("/v1/edge/status", headers=_h(["student"])).json()
    assert s["package"]["version"] == 1 and s["cache"]["hits"] == 1 and s["routes"] == {"local": 1, "cache": 1}


def test_school_node_is_read_only_and_passes_lms_through(school_node):
    c, _ = school_node
    r = c.post("/v1/knowledge/sources", headers=_h(["admin"]), files={"file": ("a.pdf", b"x")})
    assert r.status_code == 409 and "read-only school node" in r.json()["detail"]
    r = c.post("/v1/knowledge/lms/events", headers=_h(["lms_sync"]), json={"events": []})
    assert r.status_code == 503 and r.headers["Retry-After"] == "300"   # Moodle keeps it in its outbox


def test_core_first_sends_role_and_question_only(school_node, monkeypatch):
    c, calls = school_node
    seen = {}

    def core(req: httpx.Request):
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"status": "grounded", "answer": "From core.", "citations": []})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(core), **kw))
    a = c.post("/v1/knowledge/answer", headers=_h(["teacher"]), json={"question": "What is a covalent bond?"}).json()
    assert a["answer"] == "From core." and a["x_nexa"]["route"] == "core" and not calls
    assert seen["headers"]["x-nexa-act-as-role"] == "teacher"
    assert seen["body"] == {"question": "What is a covalent bond?"}            # no learner identity
    assert seen["headers"]["authorization"] == "Bearer t"                      # the school node's token, not the user's


def test_core_accepts_delegation_only_for_its_school_and_safe_roles(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EDGE_PACKAGE_DIR", str(tmp_path))
    s = asyncio.run(_core_store(corpus()))
    monkeypatch.setattr(km, "INDEX", IndexManager(s, Pipeline().registry))
    monkeypatch.setattr(embeddings, "_gateway", FakeGateway(dim=DIM))
    monkeypatch.setattr(config, "EVIDENCE_THRESHOLDS", {"default": {"min": 0.2, "strong": 0.3}})

    async def chat(messages, trace_id=None):
        return messages[1]["content"].split("\n")[1][:80] + " [S1]", "m"
    monkeypatch.setattr(grounding, "chat", chat)
    node = _h(["edge_node"], "pomnsoe")
    with TestClient(km.app) as c:
        a = c.post("/v1/knowledge/answer", headers={**node, "X-NEXA-Act-As-Role": "student"},
                   json={"question": "What does the school garden grow, kaukau and taro?", "explain": True}).json()
        used = [x["provenance"]["title"] for x in a["citations"]]
        assert "Pomnsoe garden notes" in used and "Hutjena reef notes" not in used
        r = c.post("/v1/knowledge/answer", headers={**node, "X-NEXA-Act-As-Role": "admin"},
                   json={"question": "anything"})
        assert r.status_code == 403
        r = c.post("/v1/edge/reports", headers=node, json={"version": 1, "routes": {"local": 3},
                                                           "questions": ["learner text"], "user": "s1"})
        assert r.status_code == 200
    with open(os.path.join(str(tmp_path), "reports.jsonl")) as fh:
        rec = json.loads(fh.readlines()[-1])
    assert "questions" not in rec and "user" not in rec and rec["school_id"] == "pomnsoe"


def test_probe_calls_the_gateway_with_its_real_signature(monkeypatch):
    seen = []

    async def strict(texts, model, trace_id):          # the real _gateway takes trace_id positionally
        seen.append((model, trace_id))
        return [bow_vector(t, DIM) for t in texts], {}
    monkeypatch.setattr(embeddings, "_gateway", strict)
    assert len(asyncio.run(edge_web.embed_probe(list(pkg.PROBE_TEXTS)))) == 3 and seen[0][1] is None


def _skewed(texts):
    """The same model computed by another runtime: close to the core's vectors, not equal."""
    out = []
    for t in texts:
        a, b = bow_vector(t, DIM), bow_vector("runtime drift " + t, DIM)
        out.append([0.88 * x + 0.12 * y for x, y in zip(a, b)])
    return out


def test_same_model_other_runtime_is_re_embedded_locally(tmp_path, home):
    v1 = _build(tmp_path, corpus(), 1)[0]
    embedded = []

    async def probe(texts):
        return _skewed(texts)

    async def reembed(texts):
        embedded.extend(texts)
        return _skewed(["search_document: " + t for t in texts])
    with pytest.raises(sync.SyncError, match="does not match"):           # without re-embedding: refused
        asyncio.run(sync.install_bundle(home, v1.bundle, KEY, "pomnsoe", probe))
    r = asyncio.run(sync.install_bundle(home, v1.bundle, KEY, "pomnsoe", probe, reembed=reembed))
    assert r.version == 1 and len(embedded) == v1.manifest["stats"]["chunks"]
    last = sync.history(home, 1)[-1]
    assert last["vectors"] == "local" and last["reembedded"] == len(embedded) and 0.8 < last["probe_cosine"] < 0.999
    st = EdgeStore(home)
    want = _skewed(["search_document: DNA is copied in the S (Synthesis) phase of the cell cycle."])[0]
    hit = asyncio.run(st.query("edge_pomnsoe_v1", want, 1))[0]
    assert hit["chunk_id"] == "bio-sg@v1:0" and hit["score"] > 0.999       # documents carry the local vectors
    # a delta after a local install re-embeds only the new and changed sources
    items = corpus() + _doc("chem-sg@v1", ["A covalent bond shares a pair of electrons."], subject="Chemistry")
    delta = next(x for x in _build(tmp_path, items, 2, previous=v1.manifest) if x.kind == "delta")
    embedded.clear()
    asyncio.run(sync.install_bundle(home, delta.bundle, KEY, "pomnsoe", probe, reembed=reembed))
    assert embedded == ["A covalent bond shares a pair of electrons."]


def test_a_different_model_is_refused_even_with_re_embedding(tmp_path, home):
    v1 = _build(tmp_path, corpus(), 1)[0]

    async def other(texts):
        return [bow_vector("completely unrelated model output " + t[::-1], DIM) for t in texts]
    with pytest.raises(sync.SyncError, match="not the same model"):
        asyncio.run(sync.install_bundle(home, v1.bundle, KEY, "pomnsoe", other, reembed=other))
    assert current_version(home) is None


def test_package_carries_the_exact_embedding_text(tmp_path):
    s = asyncio.run(_core_store(corpus()))
    items, _ = asyncio.run(pkg.collect(s, "idx_core", pkg.Policy(school_id="pomnsoe"),
                                       lambda cid: f"Header > Section\n\n{cid}"))
    assert items[0]["embedding_text"].startswith("Header > Section")
