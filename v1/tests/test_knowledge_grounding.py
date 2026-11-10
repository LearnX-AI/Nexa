"""NEXA-M2-006: provenance, citations and the grounding contract.

The Model Gateway's embeddings are the deterministic bag-of-words fake and
its chat model is replaced by scripted replies, so the tests decide exactly
what "the model" says, including fabricated and missing citations.
"""
from __future__ import annotations

import hashlib
import json
import re

import pytest
from fastapi.testclient import TestClient

from v1.knowledge import config, embeddings, grounding
from v1.knowledge import main as km
from v1.knowledge.indexing import IndexManager
from v1.knowledge.ingestion.pipeline import Pipeline
from v1.knowledge.store import InMemoryStore

from .knowledge_fixtures import APPROVED, FakeGateway, make_docx, make_pptx, text_pdf

THRESHOLDS = {"default": {"min": 0.30, "strong": 0.45}}


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr(embeddings, "_gateway", FakeGateway())
    monkeypatch.setattr(config, "EVIDENCE_THRESHOLDS", THRESHOLDS)
    pipe = Pipeline()
    monkeypatch.setattr(km, "PIPE", pipe)
    monkeypatch.setattr(km, "INDEX", IndexManager(InMemoryStore(), pipe.registry))
    calls: list[list[dict]] = []
    replies: list = []

    async def fake_chat(messages, trace_id=None):
        calls.append(messages)
        r = replies.pop(0) if replies else cite_first
        if isinstance(r, Exception):
            raise r
        return (r(messages) if callable(r) else r), "nexa-chat-precise"

    monkeypatch.setattr(grounding, "chat", fake_chat)
    with TestClient(km.app) as c:
        yield c, calls, replies


def cite_first(messages) -> str:
    """A well-behaved model: restates the first source's opening and cites it."""
    src = messages[1]["content"]
    first = re.search(r"\[S1\][^\n]*\n(.+)", src).group(1)
    return first[:200].rstrip(".") + ". [S1]"


def _manifest(source_id, subject, title, source_type="official_syllabus", grades=(12,), **extra):
    md = {"source_id": source_id, "source_type": source_type, "subject": subject, "grade_levels": list(grades),
          "title": title, "scope": "national", "access_classification": "learner", "publisher": "NDoE PNG",
          "source_uri": f"repo://curriculum/{source_id}", **APPROVED}
    md.update(extra)
    return {"metadata": md}


def _upload(c, auth, token, name, data, manifest):
    r = c.post("/v1/knowledge/sources", headers=auth(token), files={"file": (name, data)},
               data={"manifest": json.dumps(manifest)})
    assert r.status_code == 202, r.text
    job = c.get(f"/v1/knowledge/jobs/{r.json()['job']['job_id']}", headers=auth(token)).json()
    assert job["state"] in ("succeeded", "partial") and job["stage"] == "done", job
    return job


# Four formats, four subjects.
CASES = [
    ("chem.pdf", lambda: text_pdf([["Covalent Bonding", "A covalent bond forms when two atoms share a pair of "
                                    "electrons between them."],
                                   ["Ionic Bonding", "Ionic bonds form when electrons transfer from a metal atom "
                                    "to a non-metal atom."]]),
     _manifest("png-ndoe:chem-syllabus-gr11", "Chemistry", "Chemistry Syllabus Grade 11", grades=(11,)),
     "how do ionic bonds form between metal and non-metal atoms", "Ionic bonds form"),
    ("bio.docx", lambda: make_docx(),
     _manifest("png-ndoe:bio-guide-gr12", "Biology", "DNA Replication Teacher Guide", source_type="teacher_guide"),
     "which enzyme unwinds the DNA double helix", "Helicase"),
    ("phys.pptx", lambda: make_pptx(),
     _manifest("png-ndoe:phys-notes-gr12", "Physics", "Inductive Charging Lesson Notes", source_type="lesson_notes"),
     "how does a changing magnetic field induce a current in a coil", "magnetic field induces"),
]


def test_end_to_end_traceability_across_formats_and_subjects(svc, make_token, auth):
    c, _, _ = svc
    teacher, student = make_token(["teacher"]), make_token(["student"])
    lms = c.post("/v1/knowledge/lms-items", headers=auth(teacher), json={
        "lms": {"system": "edunex", "course_id": "stem-maths-gr12", "item_id": "cm555", "revision": "3"},
        "title": "Quadratic equations", "format": "html",
        "body": "<h2>Solving quadratics</h2><p>The quadratic formula solves any equation of the form ax squared "
                "plus bx plus c equals zero.</p>",
        "metadata": {"subject": "Mathematics", "grade_levels": [12], "scope": "national",
                     "access_classification": "learner", **APPROVED}})
    assert lms.status_code == 202
    cases = CASES + [(None, None, None, "what does the quadratic formula solve", "quadratic formula")]
    for name, build, manifest, question, expect in cases:
        if name:
            _upload(c, auth, teacher, name, build(), manifest)
        r = c.post("/v1/knowledge/answer", headers=auth(student), json={"question": question}).json()
        assert r["status"] == "grounded", (question, r["status"], r["evidence"])
        assert r["schema_version"] == grounding.CITATION_SCHEMA_VERSION
        used = [ci for ci in r["citations"] if ci["used"]]
        assert used and r["integrity"]["valid_markers"] == [u["marker"] for u in used]
        p = used[0]["provenance"]
        for k in ("source_id", "version_id", "content_checksum", "chunk_id", "run_id", "title", "index_id",
                  "embedding", "chunker_version"):
            assert p.get(k), (k, p)
        # answer -> chunk -> source version -> original bytes
        ch = c.get(f"/v1/knowledge/chunks/{p['chunk_id']}", headers=auth(student)).json()
        assert ch["provenance"]["version_id"] == p["version_id"] and ch["active"]
        assert expect.lower() in ch["text"].lower()
        assert " ".join(ch["text"].split()).startswith(used[0]["quote"])  # the quote is verbatim
        assert ch["source"]["content_checksum"] == p["content_checksum"] and ch["original"]["available"]
        orig = c.get(ch["original"]["path"], headers=auth(teacher))
        assert orig.status_code == 200
        assert "sha256:" + hashlib.sha256(orig.content).hexdigest() == p["content_checksum"]
        if name and name.endswith(".pdf"):
            # the passage is on page 2 of the PDF
            assert p["page_start"] <= 2 <= (p["page_end"] or p["page_start"]) and p["page_basis"] == "physical"
        if name is None:
            assert p["lms"]["item_id"] == "cm555" and p["lms"]["revision"] == "3"


def test_provenance_survives_rerank_and_context(svc, make_token, auth):
    c, calls, _ = svc
    teacher, student = make_token(["teacher"]), make_token(["student"])
    _upload(c, auth, teacher, "bio.docx", make_docx(extra_paragraphs=6), CASES[1][2])
    q = c.post("/v1/knowledge/query", headers=auth(student),
               json={"query": "lagging strand Okazaki fragments", "top_k": 5, "rerank": True}).json()
    for r in q["results"]:
        reg = km.PIPE.registry.get_chunk(r["chunk_id"])
        assert reg and r["provenance"]["version_id"] == reg["version_id"]
        assert r["provenance"]["run_id"] == reg["run_id"] and r["text"] == reg["text"]
    a = c.post("/v1/knowledge/answer", headers=auth(student),
               json={"question": "lagging strand Okazaki fragments"}).json()
    ctx = calls[-1][1]["content"]
    for ci in a["citations"]:
        # Each marker in the context is followed by exactly that chunk's text.
        block = ctx.split(f"[{ci['marker']}] ", 1)[1].split("\n", 1)[1].split("\n\n[S", 1)[0]
        assert block[:80] == " ".join(km.PIPE.registry.get_chunk(ci["provenance"]["chunk_id"])["text"].split())[:80]


def test_fabricated_citations_are_removed_and_reported(svc, make_token, auth):
    c, _, replies = svc
    teacher, student = make_token(["teacher"]), make_token(["student"])
    _upload(c, auth, teacher, "bio.docx", make_docx(), CASES[1][2])
    replies.append("Helicase unwinds the DNA double helix [S1][S9]. Primase makes primers [4]. See [S2, S7].")
    a = c.post("/v1/knowledge/answer", headers=auth(student),
               json={"question": "which enzyme unwinds the DNA double helix"}).json()
    assert a["status"] == "grounded"
    assert "S9" in a["integrity"]["fabricated_markers"] and "4" in a["integrity"]["fabricated_markers"]
    assert "S7" in a["integrity"]["fabricated_markers"]
    assert "[S9]" not in a["answer"] and "[4]" not in a["answer"] and "[S7]" not in a["answer"]
    assert "[S1]" in a["answer"]
    assert any("did not match a source" in n for n in a["notes"])
    # Only invented references: the answer is withheld, not shown without support.
    replies.append("Helicase unwinds DNA [S12]. This is well known [7].")
    a = c.post("/v1/knowledge/answer", headers=auth(student),
               json={"question": "which enzyme unwinds the DNA double helix"}).json()
    assert a["status"] == "ungrounded" and a["answer"] == grounding.FALLBACK_UNGROUNDED
    assert a["confidence"] == "none" and a["citations"]  # the passages are still listed
    replies.append("Helicase unwinds the DNA double helix.")  # no citation at all
    a = c.post("/v1/knowledge/answer", headers=auth(student),
               json={"question": "which enzyme unwinds the DNA double helix"}).json()
    assert a["status"] == "ungrounded"


def test_no_evidence_is_a_controlled_fallback_without_a_model_call(svc, make_token, auth):
    c, calls, _ = svc
    teacher, student = make_token(["teacher"]), make_token(["student"])
    _upload(c, auth, teacher, "bio.docx", make_docx(), CASES[1][2])
    n = len(calls)
    a = c.post("/v1/knowledge/answer", headers=auth(student),
               json={"question": "Who won the rugby league grand final in Port Moresby last year?"}).json()
    assert a["status"] == "no_evidence" and a["answer"] == grounding.FALLBACK_NO_EVIDENCE
    assert a["confidence"] == "none" and len(calls) == n and a["citations"] == []
    # Filtered to nothing: also no evidence.
    a = c.post("/v1/knowledge/answer", headers=auth(student),
               json={"question": "which enzyme unwinds DNA", "filters": {"subjects": ["Physics"]}}).json()
    assert a["status"] == "no_evidence" and a["evidence"]["passages"] == 0
    # The model itself says the passages do not answer.
    _, _, replies = svc
    replies.append("INSUFFICIENT_EVIDENCE")
    a = c.post("/v1/knowledge/answer", headers=auth(student),
               json={"question": "which enzyme unwinds the DNA double helix"}).json()
    assert a["status"] == "no_evidence" and a["model"] == "nexa-chat-precise"


def test_weak_evidence_is_marked_low_confidence(svc, make_token, auth, monkeypatch):
    c, _, _ = svc
    teacher, student = make_token(["teacher"]), make_token(["student"])
    _upload(c, auth, teacher, "bio.docx", make_docx(), CASES[1][2])
    monkeypatch.setattr(config, "EVIDENCE_THRESHOLDS", {"default": {"min": 0.05, "strong": 0.99}})
    monkeypatch.setattr(config, "EVIDENCE_COVERAGE_STRONG", 1.01)
    a = c.post("/v1/knowledge/answer", headers=auth(student),
               json={"question": "what happens to the strands at the fork"}).json()
    assert a["status"] == "grounded_weak" and a["confidence"] == "low"
    assert grounding.NOTE_WEAK in a["notes"] and a["evidence"]["level"] == "weak"


def test_conflicting_sources_follow_the_official_one(svc, make_token, auth):
    c, calls, _ = svc
    teacher, student = make_token(["teacher"]), make_token(["student"])
    official = make_docx(title="Biology Syllabus", body=[("Normal", "Helicase unwinds the DNA double helix "
                                                                    "during replication in the S phase.")])
    _upload(c, auth, teacher, "syl.docx", official,
            _manifest("png-ndoe:bio-syl", "Biology", "Biology Syllabus", curriculum={"topic_code": "12.2.3"}))
    lms = c.post("/v1/knowledge/lms-items", headers=auth(teacher), json={
        "lms": {"system": "edunex", "course_id": "bio12", "item_id": "cm901"}, "title": "Helicase notes",
        "body": "<p>Helicase unwinds the DNA double helix by breaking hydrogen bonds between bases.</p>",
        "metadata": {"subject": "Biology", "grade_levels": [12], "scope": "national",
                     "access_classification": "learner", "curriculum": {"topic_code": "12.2.3"}, **APPROVED}})
    assert lms.status_code == 202
    a = c.post("/v1/knowledge/answer", headers=auth(student), json={
        "question": "how does helicase unwind the DNA double helix by breaking hydrogen bonds", "explain": True}).json()
    assert a["evidence"]["conflict"] and grounding.NOTE_CONFLICT in a["notes"]
    assert a["citations"][0]["provenance"]["source_type"] == "official_syllabus"
    lms_cite = next(ci for ci in a["citations"] if ci["provenance"]["source_type"] == "lms_content")
    assert a["citations"][0]["authoritative"] and not lms_cite["authoritative"]
    ctx = calls[-1][1]["content"]
    assert "(official_syllabus, authoritative)" in ctx and "(lms_content, supplementary)" in ctx
    assert a["retrieval"]["conflicts"]


def test_generation_failure_returns_passages(svc, make_token, auth):
    c, _, replies = svc
    teacher, student = make_token(["teacher"]), make_token(["student"])
    _upload(c, auth, teacher, "bio.docx", make_docx(), CASES[1][2])
    replies.append(RuntimeError("gateway 503"))
    a = c.post("/v1/knowledge/answer", headers=auth(student),
               json={"question": "which enzyme unwinds the DNA double helix"}).json()
    assert a["status"] == "generation_unavailable" and a["citations"] and a["confidence"] == "none"


def test_chunk_trace_respects_access(svc, make_token, auth):
    c, _, _ = svc
    teacher, student = make_token(["teacher"]), make_token(["student"])
    job = _upload(c, auth, teacher, "key.docx", make_docx(title="Answer key"),
                  _manifest("png-ndoe:bio-key", "Biology", "Answer key", source_type="assessment",
                            access_classification="teacher_only"))
    cid = km.PIPE.registry.chunks_of(job["version_id"])[0]["chunk_id"]
    assert c.get(f"/v1/knowledge/chunks/{cid}", headers=auth(student)).status_code == 404
    assert c.get(f"/v1/knowledge/chunks/{cid}", headers=auth(teacher)).status_code == 200
    assert c.get(f"/v1/knowledge/sources/{job['version_id']}/original", headers=auth(student)).status_code == 403
    assert c.get("/v1/knowledge/chunks/nope", headers=auth(teacher)).status_code == 404


def test_citation_schema_is_published(svc, make_token, auth):
    c, _, _ = svc
    s = c.get("/v1/knowledge/schema/grounded-answer", headers=auth(make_token(["student"]))).json()
    assert s["$id"].endswith(f"/{grounding.CITATION_SCHEMA_VERSION}.json")
    assert {"status", "citations", "evidence", "integrity"} <= set(s["properties"])


def test_validate_marker_forms_and_support():
    used = [{"marker": "S1", "item": {"text": "Helicase unwinds the DNA double helix."}, "authoritative": True},
            {"marker": "S2", "item": {"text": "Primase synthesises short RNA primers."}, "authoritative": True}]
    text, integ, support = grounding.validate(
        "Helicase unwinds the double helix [S1, S2]. Rainfall is high in Lae every single year [S2].", used)
    assert integ.valid_markers == ["S1", "S2"] and "[S1][S2]" in text
    assert integ.weakly_supported_sentences == 1  # the rainfall sentence does not match S2
    assert support["S1"] > support["S2"]
    text, integ, _ = grounding.validate("Helicase unwinds DNA [s1] (see [S3]).", used)
    assert integ.valid_markers == ["S1"] and integ.fabricated_markers == ["S3"] and "[S3]" not in text
