"""Answer quality: spelling, fuller course outlines, honest confidence and
labelled general content inside curriculum answers."""
from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from v1.identity.principal import Principal
from v1.knowledge import config, embeddings, grounding, outline, spelling
from v1.knowledge import main as km
from v1.knowledge.access import retrieval_filter
from v1.knowledge.indexing import IndexManager
from v1.knowledge.ingestion.pipeline import Pipeline
from v1.knowledge.store import IndexRecord, InMemoryStore

from .knowledge_fixtures import FakeGateway, bow_vector

BASE = {"approval_status": "approved", "access_classification": "learner", "effective_from": "2022-01-01",
        "language": "en", "scope": "national"}


def _doc(vid, source_type, title, sections, subject="Biology", grade=12, term=None, extra=None):
    out = []
    for i, (path, text) in enumerate(sections):
        cid = f"{vid}:{i}"
        md = {**BASE, "source_type": source_type, "subject": subject, "grade_levels": str(grade), "title": title,
              "version_id": vid, "source_id": vid.split("@")[0], "chunk_id": cid, "ordinal": i,
              "section_path": path, "page_label": str(i + 3), "citation": f"{title}, p. {i + 3}", **(extra or {})}
        if term is not None:
            md["term"] = term
        out.append({"chunk_id": cid, "version_id": vid, "text": text, "vector": bow_vector(text, 128),
                    "metadata": md})
    return out


ITEMS = (
    _doc("syl-b12-t2@v1", "official_syllabus", "STEM Biology Syllabus, Grade 12, Term 2", [
        ("Thema: Energy > Module 12.1: Bioenergy", "Bioenergy and renewable energy in PNG."),
        ("Thema: Battery Technology > Module 12.1: Bio-Batteries", "Bio-batteries."),
    ], term=2, extra={"authority_tier": 1})
    + _doc("sg-b12-t1@v1", "student_guide", "STEM Biology Student Guide, Grade 12, Term 1", [
        ("Thema: Health and Medicine > Module 12.1: Health and Medical Biology", "Homeostasis keeps conditions "
         "stable. Cells divide by mitosis to grow and repair tissue. Mitosis gives two identical cells."),
        ("Thema: Health and Medicine > Module 12.1: Health and Medical Biology > Topic 1: Homeostasis",
         "Meiosis produces gametes with half the chromosomes. Meiosis happens in the ovaries and testes."),
    ], term=1)
    + _doc("tg-b12-t3@v1", "teacher_guide", "STEM Biology Teacher Guide, Grade 12, Term 3", [
        ("Thema: Intelligence > Module 12.1: Neuroscience > Topic 1: The Nervous System", "Neurons."),
    ], term=3, extra={"access_classification": "teacher_only"})
    + _doc("lms-cm8870@v1", "lms_content", "DNA Replication (Lesson Notes, Genetics and Molecular Biology)", [
        ("", "Photosynthesis in plants makes glucose. DNA is copied in the S phase."),
    ], extra={"publisher": "Biology Grade 12", "source_uri": "lms://edunex/cm8870"})
    + _doc("lms-cm8871@v1", "lms_content", "Photosynthesis_PPT", [
        ("", "Photosynthesis converts light energy into chemical energy in plants."),
    ], extra={"publisher": "Biology Grade 12", "source_uri": "lms://edunex/cm8871"})
    + _doc("syl-p12-t1@v1", "official_syllabus", "STEM Physics Syllabus, Grade 12, Term 1", [
        ("Thema: Energy > Module 12.1: Nuclear Energy", "Nuclear energy."),
    ], subject="Physics", term=1, extra={"authority_tier": 1})
)


@pytest.fixture
def mgr(monkeypatch):
    outline.clear_cache()
    spelling.clear_cache()
    monkeypatch.setattr(config, "SPELL_MIN_FREQ", 2)
    store = InMemoryStore()

    async def load():
        await store.create_index(IndexRecord("idx_q", "nexa-embed-default", "nomic-embed-text", "ollama", 128,
                                             "nomic-embed"))
        await store.set_active("idx_q")
        await store.upsert("idx_q", [dict(i) for i in ITEMS])
    asyncio.run(load())
    pipe = Pipeline()
    return IndexManager(store, pipe.registry), pipe


# ---------------------------------------------------------------- spelling

def test_spelling_suggestions_from_the_curriculum_vocabulary():
    v = Counter({"mitosis": 5, "meiosis": 4, "photosynthesis": 9, "homeostasis": 6, "homeostatic": 3,
                 "gene": 8, "genes": 12})
    assert spelling.suggest("metosis", v) == ["mitosis", "meiosis"]          # equally close: ask
    assert spelling.suggest("photosinthesis", v) == ["photosynthesis"]
    assert spelling.suggest("homeostatis", v) == ["homeostasis"]           # one word family
    assert spelling.suggest("genez", v) == ["genes"]
    assert spelling.suggest("rugby", v) == []
    assert spelling.distance("teh", "the", 1) == 1                         # a swap is one edit


def test_misspelt_word_is_corrected_and_ambiguous_word_asks(mgr):
    m, _ = mgr
    c = asyncio.run(spelling.check(m, "How does photosinthesis work in plants?"))
    assert c.corrections == {"photosinthesis": "photosynthesis"} and "photosynthesis work" in c.corrected
    c = asyncio.run(spelling.check(m, "What is metosis"))
    assert set(c.ambiguous["metosis"]) == {"mitosis", "meiosis"} and not c.corrections
    c = asyncio.run(spelling.check(m, "Who won the rugby league last year?"))
    assert not c.corrections and not c.ambiguous


def test_answer_endpoint_spelling(mgr, monkeypatch, make_token, auth):
    m, pipe = mgr
    monkeypatch.setattr(embeddings, "_gateway", FakeGateway())
    monkeypatch.setattr(km, "PIPE", pipe)
    monkeypatch.setattr(km, "INDEX", m)
    monkeypatch.setattr(config, "EVIDENCE_THRESHOLDS", {"default": {"min": 0.10, "strong": 0.20}})
    asked = []

    async def chat(messages, trace_id=None):
        asked.append(messages[1]["content"])
        return "Photosynthesis converts light energy into chemical energy [S1].", "nexa-chat-precise"
    monkeypatch.setattr(grounding, "chat", chat)
    with TestClient(km.app) as c:
        h = auth(make_token(["student"]))
        a = c.post("/v1/knowledge/answer", headers=h, json={"question": "Explain photosinthesis"}).json()
        assert a["answer"].startswith("Showing results for 'photosynthesis' (you typed 'photosinthesis').")
        assert a["spelling"]["corrections"] == {"photosinthesis": "photosynthesis"}
        assert "Question: Explain photosynthesis" in asked[-1]
        n = len(asked)
        a = c.post("/v1/knowledge/answer", headers=h, json={"question": "What is metosis"}).json()
        assert a["status"] == "clarification_needed" and "Did you mean" in a["answer"]
        assert "'mitosis'" in a["answer"] and "'meiosis'" in a["answer"] and len(asked) == n  # no model call


# ---------------------------------------------------------------- outlines

def test_outline_uses_guides_for_terms_without_a_syllabus_and_the_lms_course(mgr):
    m, _ = mgr
    student = retrieval_filter(Principal("s", ("student",), "user"))
    rr, _, notes = asyncio.run(outline.build(m, student, "What are the topics in Grade 12 Biology"))
    cites = [r["metadata"]["citation"] for r in rr.results]
    assert cites[0].startswith("STEM Biology Student Guide, Grade 12, Term 1")
    assert cites[1].startswith("STEM Biology Syllabus, Grade 12, Term 2")
    assert cites[2] == "EduNex course: Biology Grade 12"
    assert "from the Student Guide" in rr.results[0]["text"] and "Topic 1: Homeostasis" in rr.results[0]["text"]
    lms = rr.results[2]["text"]
    assert "Unit: Genetics and Molecular Biology\n  - DNA Replication" in lms and "Photosynthesis" in lms
    assert rr.results[0]["authority"]["authoritative"] is False and rr.results[1]["authority"]["authoritative"]
    # Term 3 exists only in a teacher-only guide: a student is told no outline is available
    assert notes == ["Grade 12 Biology in NEXA: Term 1 from the Student Guide, Term 2 from the syllabus, "
                     "the EduNex course lessons. No outline available yet for Term 3."]
    teacher = retrieval_filter(Principal("t", ("teacher",), "user"))
    rr, _, notes = asyncio.run(outline.build(m, teacher, "What are the topics in Grade 12 Biology"))
    assert any("Teacher Guide, Grade 12, Term 3" in r["metadata"]["citation"] for r in rr.results)
    assert "Term 3 from the Teacher Guide" in notes[0] and "No outline available" not in notes[0]


# ---------------------------------------------------------------- confidence and general content

P = [{"marker": "S1", "item": {"text": "Malaria is a parasite spread by the bites of infected mosquitoes. It is a "
                                       "major health concern in Papua New Guinea."}}]


def test_reference_lines_the_model_echoes_are_removed():
    p = [{"marker": "S1", "item": {"text": "12.1.1c: Clearly explain how electromagnets and transformers utilize "
                                           "electromagnetic induction."}}]
    text, integ, _ = grounding.validate(
        "The indicator is 12.1.1c: Clearly explain how electromagnets and transformers utilize electromagnetic "
        "induction [S1].\n\n[S1] STEM Physics Syllabus, Grade 12, Term 2, p. 22", p)
    assert "p. 22" not in text and integ.reference_lines_removed == 1 and integ.weakly_supported_sentences == 0


def test_unsupported_uncited_paragraphs_are_labelled_as_general():
    ans = ("Malaria is spread by the bites of infected mosquitoes [S1].\n\n"
           "To prevent it, sleep under insecticide-treated bed nets, use repellents and drain stagnant water.")
    text, integ, _ = grounding.validate(ans, P)
    first, rest = text.split(grounding.GENERAL_LABEL)
    assert "[S1]" in first and "bed nets" in rest and "bed nets" not in first and integ.general_paragraphs == 1


def test_supported_summary_paragraph_stays():
    ans = ("Malaria is spread by the bites of infected mosquitoes [S1].\n\n"
           "So malaria, a major health concern in Papua New Guinea, spreads through infected mosquito bites.")
    text, integ, _ = grounding.validate(ans, P)
    assert grounding.GENERAL_LABEL not in text and integ.general_paragraphs == 0


@pytest.mark.parametrize("hedge", [
    "In terms of specific cell types, there isn't much information provided in the sources [S1].",
    "Unfortunately, the sources do not mention this term [S1].",
    "I can only answer questions based on the provided sources [S1].",
])
def test_an_answer_that_says_the_sources_fall_short_is_marked_partial(hedge, monkeypatch):
    from v1.knowledge.retrieval import RetrievalResult
    rr = RetrievalResult(results=[{"chunk_id": "c1", "text": P[0]["item"]["text"], "rank": 1, "score": 0.9,
                                   "scores": {"similarity": 0.9}, "coverage": {"coverage": 1.0},
                                   "metadata": {**BASE, "title": "Lesson", "citation": "Lesson, p. 1",
                                                "source_type": "lms_content"},
                                   "authority": {"authoritative": False, "notes": []}}],
                         explain={"conflicts": []}, index=None)
    monkeypatch.setattr(config, "EVIDENCE_THRESHOLDS", {"default": {"min": 0.3, "strong": 0.5}})

    async def chat(messages, trace_id=None):
        return "Malaria is spread by the bites of infected mosquitoes [S1]. " + hedge, "m"
    monkeypatch.setattr(grounding, "chat", chat)
    a = asyncio.run(grounding.answer("Explain malaria", rr))
    assert a.status == "grounded_weak" and a.confidence == "low" and a.integrity.hedged
    assert grounding.NOTE_WEAK in a.notes
