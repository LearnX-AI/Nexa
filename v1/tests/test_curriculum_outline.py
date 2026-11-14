"""Curriculum outline answers ("what are the topics in Grade 11 Biology?")."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from v1.identity.principal import Principal
from v1.knowledge import config, embeddings, grounding, outline
from v1.knowledge import main as km
from v1.knowledge.access import retrieval_filter
from v1.knowledge.indexing import IndexManager
from v1.knowledge.ingestion.pipeline import Pipeline
from v1.knowledge.store import IndexRecord, InMemoryStore

from .knowledge_fixtures import FakeGateway, bow_vector

BASE = {"approval_status": "approved", "access_classification": "learner", "effective_from": "2022-01-01",
        "language": "en", "scope": "national", "source_type": "official_syllabus", "authority_tier": 1}


def _syllabus(subject, grade, term, sections):
    title = f"STEM {subject} Syllabus, Grade {grade}, Term {term}"
    vid = f"png-ndoe:{subject.lower()}-g{grade}-t{term}@v1"
    out = []
    for i, (path, text) in enumerate(sections):
        cid = f"{vid}:{i}"
        out.append({"chunk_id": cid, "version_id": vid, "text": text, "vector": bow_vector(text, 128),
                    "metadata": {**BASE, "subject": subject, "grade_levels": str(grade), "term": term,
                                 "title": title, "version_id": vid, "source_id": vid.split("@")[0],
                                 "chunk_id": cid, "ordinal": i, "section_path": path, "page_label": str(i + 1),
                                 "citation": f"{title}, p. {i + 1}"}})
    return out


ITEMS = (
    _syllabus("Biology", 11, 2, [
        ("SECRETARY'S MESSAGE", "This syllabus is to be used by teachers of Biology Grade 11."),
        ("Stem Biology Content Overview – Term 2", "Table 1.1 Thema sequence: Biodiversity (3 weeks), Biotechnology."),
        ("Thema: Biodiversity > Module 11.1: Agricultural Biodiversity", "Agricultural biodiversity in PNG."),
        ("Thema: Biodiversity > Module 11.2: Ornithology", "Birds of paradise."),
        ("Thema: Biodiversity > Module 11.2: Ornithology", "Nesting behaviour."),
        ("Thema: Biotechnology > Module 11.1: Concepts and Applications of Biotechnology", "Recombinant DNA."),
    ])
    + _syllabus("Biology", 11, 3, [
        ("Thema: Energy > Module: Biological Energy Systems", "Energy in plant and animal cells."),
    ])
    + _syllabus("Physics", 12, 1, [("Thema: Biotechnology > Module 12.1: Biophysics", "Biophysics.")])
    + _syllabus("Physics", 12, 2, [("Thema: Energy > Module 12.1: Nuclear Energy", "Nuclear energy.")])
)


@pytest.fixture
def mgr():
    outline.clear_cache()
    store = InMemoryStore()

    async def load():
        await store.create_index(IndexRecord("idx_outline", "nexa-embed-default", "nomic-embed-text", "ollama",
                                             128, "nomic-embed"))
        await store.set_active("idx_outline")
        await store.upsert("idx_outline", [dict(i) for i in ITEMS])
    asyncio.run(load())
    pipe = Pipeline()
    return IndexManager(store, pipe.registry), pipe


STUDENT = retrieval_filter(Principal("s", ("student",), "user"))


@pytest.mark.parametrize("q,subject,grade,term", [
    ("What are the topics in Grade 11 Biology?", "Biology", 11, None),
    ("What units are covered in Grade 12 Physics Term 2?", "Physics", 12, 2),
    ("What will we learn in grade 11 biology?", "Biology", 11, None),
    ("List the modules for Physics", "Physics", None, None),
])
def test_detects_outline_questions(q, subject, grade, term):
    a = outline.detect(q, {"Biology", "Physics", "Chemistry"})
    assert (a.subject, a.grade, a.term) == (subject, grade, term)


@pytest.mark.parametrize("q", ["What is photosynthesis?", "Which outcome covers naming the 20 amino acids?",
                               "During which phase of the cell cycle is DNA copied?",
                               "What are the topics in Grade 11 History?"])
def test_ordinary_questions_are_not_outline_questions(q):
    assert outline.detect(q, {"Biology", "Physics", "Chemistry"}) is None


def test_outline_lists_every_module_in_order_and_names_missing_terms(mgr):
    m, _ = mgr
    rr, question, notes = asyncio.run(outline.build(m, STUDENT, "What are the topics in Grade 11 Biology?"))
    assert [r["metadata"]["title"] for r in rr.results] == ["STEM Biology Syllabus, Grade 11, Term 2",
                                                            "STEM Biology Syllabus, Grade 11, Term 3"]
    t2 = rr.results[0]["text"]
    assert t2.index("Agricultural Biodiversity") < t2.index("Ornithology") < t2.index("Concepts and Applications")
    assert t2.count("Ornithology") == 1 and "Thema: Biotechnology" in t2 and "Table 1.1" in t2
    assert "from the syllabus" in t2
    assert "Module: Biological Energy Systems" in rr.results[1]["text"]
    # Term 1 exists elsewhere in the index (Physics), so it is reported missing for Grade 11 Biology
    assert notes == ["Grade 11 Biology in NEXA: Terms 2, 3 from the syllabus. No outline available yet for Term 1."]
    assert "Do not add topics" in question
    # cited to a real chunk of each syllabus
    assert rr.results[0]["chunk_id"].startswith("png-ndoe:biology-g11-t2@v1:")


def test_outline_respects_access(mgr):
    m, _ = mgr
    restricted = [i for i in ITEMS if i["metadata"]["subject"] == "Physics"]
    for i in restricted:
        i["metadata"]["access_classification"] = "teacher_only"
    try:
        outline.clear_cache()
        asyncio.run(m.store.upsert("idx_outline", [dict(i) for i in restricted]))
        assert asyncio.run(outline.build(m, STUDENT, "What are the topics in Grade 12 Physics?")) is None
        teacher = retrieval_filter(Principal("t", ("teacher",), "user"))
        assert asyncio.run(outline.build(m, teacher, "What are the topics in Grade 12 Physics?")) is not None
    finally:
        for i in restricted:
            i["metadata"]["access_classification"] = "learner"


def test_answer_endpoint_gives_a_cited_outline_answer(mgr, monkeypatch, make_token, auth):
    m, pipe = mgr
    monkeypatch.setattr(embeddings, "_gateway", FakeGateway())
    monkeypatch.setattr(km, "PIPE", pipe)
    monkeypatch.setattr(km, "INDEX", m)
    seen = []

    async def chat(messages, trace_id=None):
        seen.append(messages)
        return ("Term 2 covers Biodiversity: Agricultural Biodiversity and Ornithology [S1]. "
                "Term 3 covers Energy: Biological Energy Systems [S2]."), "nexa-chat-precise"
    monkeypatch.setattr(grounding, "chat", chat)
    with TestClient(km.app) as c:
        h = auth(make_token(["student"]))
        a = c.post("/v1/knowledge/answer", headers=h, json={"question": "What are the topics in Grade 11 Biology?",
                                                             "explain": True}).json()
        assert a["status"] == "grounded" and a["source_kind"] == "curriculum"
        assert a["retrieval"]["mode"] == "curriculum_outline"
        assert [x["provenance"]["title"] for x in a["citations"] if x["used"]] == [
            "STEM Biology Syllabus, Grade 11, Term 2", "STEM Biology Syllabus, Grade 11, Term 3"]
        assert a["answer"].endswith("No outline available yet for Term 1.")
        assert "Module 11.2: Ornithology" in seen[0][1]["content"]
        monkeypatch.setattr(config, "CURRICULUM_OUTLINE", False)
        a = c.post("/v1/knowledge/answer", headers=h, json={"question": "What are the topics in Grade 11 Biology?",
                                                             "explain": True}).json()
        assert (a.get("retrieval") or {}).get("mode") != "curriculum_outline"
