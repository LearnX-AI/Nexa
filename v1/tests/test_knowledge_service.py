"""NEXA-M2 Knowledge Service endpoints: upload, jobs, retry, indexing, access-filtered query.

The Model Gateway is replaced by a deterministic bag-of-words embedder, so no
network is used.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

import pytest
from fastapi.testclient import TestClient

from v1.knowledge import embeddings
from v1.knowledge import main as km
from v1.knowledge.ingestion.pipeline import Pipeline
from v1.knowledge.store import InMemoryStore

from .knowledge_fixtures import lesson_manifest, make_docx, make_pptx, minimal_pdf


def _vec(text: str, dim: int = 128) -> list[float]:
    v = [0.0] * dim
    for w in re.findall(r"[a-z]{3,}", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dim] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


@pytest.fixture
def svc(monkeypatch):
    calls = {"n": 0, "fail": False}

    async def fake_embed(texts, trace_id=None):
        calls["n"] += 1
        if calls["fail"]:
            raise embeddings.EmbeddingError("gateway 503")
        return [_vec(t) for t in texts]

    monkeypatch.setattr(embeddings, "embed", fake_embed)
    monkeypatch.setattr(km, "PIPE", Pipeline())
    monkeypatch.setattr(km, "_STORE", InMemoryStore())
    with TestClient(km.app) as c:
        yield c, calls


def _upload(c, token, auth, name, data, manifest):
    return c.post("/v1/knowledge/sources", headers=auth(token),
                  files={"file": (name, data)}, data={"manifest": json.dumps(manifest)})


def test_upload_processes_indexes_and_answers_with_citation(svc, make_token, auth):
    c, _ = svc
    teacher = make_token(["teacher"])
    r = _upload(c, teacher, auth, "lesson.docx", make_docx(extra_paragraphs=3), lesson_manifest())
    assert r.status_code == 202 and r.json()["created"]
    job = c.get(f"/v1/knowledge/jobs/{r.json()['job']['job_id']}", headers=auth(teacher)).json()
    assert job["state"] == "succeeded" and job["stage"] == "done"
    q = c.post("/v1/knowledge/query", headers=auth(make_token(["student"])),
               json={"query": "which enzyme unwinds the DNA double helix", "top_k": 3}).json()
    top = q["results"][0]
    assert "Helicase" in top["text"]
    md = top["metadata"]
    assert top["citation"].startswith("DNA Replication (Lesson Notes), p. 1 (approx.)")
    assert md["source_id"] == "edunex:cm9001" and md["chunker_version"] and md["authority_tier"] == 4
    again = _upload(c, teacher, auth, "lesson.docx", make_docx(extra_paragraphs=3), lesson_manifest())
    assert again.json()["created"] is False


def test_rbac_and_validation_errors(svc, make_token, auth):
    c, _ = svc
    r = _upload(c, make_token(["student"]), auth, "a.docx", make_docx(), lesson_manifest())
    assert r.status_code == 403
    teacher = make_token(["teacher"])
    assert _upload(c, teacher, auth, "a.docx", make_docx(), {"bogus": 1}).status_code == 422
    assert _upload(c, teacher, auth, "a.docx", make_docx(),
                   {"chunking": {"target_tokens": 5}}).status_code == 422
    bad = _upload(c, teacher, auth, "x.pdf", minimal_pdf(b"/JavaScript (x)"), {})
    assert bad.status_code == 202 and bad.json()["job"]["state"] == "quarantined"
    assert c.get("/v1/knowledge/jobs?state=quarantined", headers=auth(teacher)).json()["jobs"]
    assert c.get("/v1/knowledge/jobs/job_missing", headers=auth(teacher)).status_code == 404
    assert c.get("/v1/knowledge/schema/source-metadata", headers=auth(teacher)).json()["title"].startswith("NEXA")


def test_pending_source_not_indexed_until_approved(svc, make_token, auth):
    c, _ = svc
    teacher = make_token(["teacher"])
    m = lesson_manifest()
    m["metadata"].pop("approval_status")
    m["metadata"].pop("approved_by")
    jid = _upload(c, teacher, auth, "l.docx", make_docx(), m).json()["job"]["job_id"]
    job = c.get(f"/v1/knowledge/jobs/{jid}", headers=auth(teacher)).json()
    assert job["stage"] == "index" and any("not indexed" in w for w in job["warnings"])
    student = make_token(["student"])
    assert c.post("/v1/knowledge/query", headers=auth(student), json={"query": "helicase"}).json()["results"] == []
    r = c.post(f"/v1/knowledge/jobs/{jid}/retry", headers=auth(teacher),
               json={"metadata_patch": {"approval_status": "approved", "approved_by": "head-of-science"}})
    assert r.status_code == 202
    assert c.get(f"/v1/knowledge/jobs/{jid}", headers=auth(teacher)).json()["stage"] == "done"
    assert c.post("/v1/knowledge/query", headers=auth(student), json={"query": "helicase"}).json()["results"]


def test_teacher_only_material_hidden_from_students(svc, make_token, auth):
    c, _ = svc
    admin = make_token(["platform_admin"])
    m = {"metadata": {"source_id": "png-ndoe:stem-physics-teacher-guide-gr12-t2", "source_type": "teacher_guide",
                      "subject": "Physics", "grade_levels": [12], "term": 2, "scope": "national",
                      "publisher": "PNG National Department of Education", "title": "Inductive Charging TG",
                      "access_classification": "teacher_only", "approval_status": "approved",
                      "approved_by": "content-team", "effective_from": "2024-01-01",
                      "source_uri": "repo://teacher-guide.pptx"}}
    assert _upload(c, admin, auth, "tg.pptx", make_pptx(), m).status_code == 202
    q = {"query": "magnetic field induces a current in a coil"}
    student = c.post("/v1/knowledge/query", headers=auth(make_token(["student"])), json=q).json()
    teacher = c.post("/v1/knowledge/query", headers=auth(make_token(["teacher"])), json=q).json()
    service = c.post("/v1/knowledge/query", headers=auth(make_token(["service"], token_type="service")),
                     json=q).json()
    assert student["results"] == [] and service["results"] == []
    assert teacher["results"] and teacher["results"][0]["metadata"]["page_basis"] == "slide"


def test_index_failure_is_visible_and_retryable(svc, make_token, auth):
    c, calls = svc
    teacher = make_token(["teacher"])
    calls["fail"] = True
    jid = _upload(c, teacher, auth, "l.docx", make_docx(), lesson_manifest()).json()["job"]["job_id"]
    job = c.get(f"/v1/knowledge/jobs/{jid}", headers=auth(teacher)).json()
    assert job["state"] == "failed" and job["stage"] == "index" and job["retryable"]
    assert job["error_code"] == "index_error"
    calls["fail"] = False
    c.post(f"/v1/knowledge/jobs/{jid}/retry", headers=auth(teacher))
    job = c.get(f"/v1/knowledge/jobs/{jid}", headers=auth(teacher)).json()
    assert job["state"] == "succeeded" and job["stage"] == "done"


def test_new_version_replaces_old_vectors(svc, make_token, auth):
    c, _ = svc
    teacher = make_token(["teacher"])
    _upload(c, teacher, auth, "l.docx", make_docx(), lesson_manifest())
    v2 = make_docx(body=[("Heading 1", "DNA Replication (revised)"),
                         ("Normal", "Topoisomerase relieves supercoiling ahead of the replication fork.")],
                   with_table=False)
    _upload(c, teacher, auth, "l.docx", v2, lesson_manifest(effective_from="2025-06-01"))
    res = c.post("/v1/knowledge/query", headers=auth(make_token(["student"])),
                 json={"query": "helicase unwinds", "top_k": 10}).json()["results"]
    assert res and all("Helicase" not in r["text"] for r in res)
    srcs = c.get("/v1/knowledge/sources?status=superseded", headers=auth(teacher)).json()["sources"]
    assert len(srcs) == 1 and srcs[0]["source_id"] == "edunex:cm9001"
    vid = srcs[0]["version_id"]
    detail = c.get(f"/v1/knowledge/sources/{vid}", headers=auth(teacher)).json()
    assert detail["runs"] and c.get(f"/v1/knowledge/sources/{vid}/chunks?active=false",
                                    headers=auth(teacher)).json()["chunks"]


def test_lms_push_endpoint(svc, make_token, auth):
    c, _ = svc
    body = {"lms": {"course_id": "stem-bio-gr12", "item_id": "cm777"}, "title": "Biogas",
            "body": "<h1>Biogas</h1><p>Biogas digesters work slowly in cold conditions.</p>",
            "metadata": {"subject": "Biology", "grade_levels": [12], "scope": "national",
                         "access_classification": "learner", "approval_status": "approved",
                         "approved_by": "content-team", "effective_from": "2024-01-01"}}
    r = c.post("/v1/knowledge/lms-items", headers=auth(make_token(["teacher"])), json=body)
    assert r.status_code == 202
    res = c.post("/v1/knowledge/query", headers=auth(make_token(["student"])),
                 json={"query": "why are biogas digesters slow"}).json()["results"]
    assert res and res[0]["metadata"]["source_uri"] == "edunex://course/stem-bio-gr12/item/cm777"


def test_health_reports_versions(svc):
    c, _ = svc
    h = c.get("/health").json()
    assert {"metadata_schema", "pipeline", "normalizer", "chunker"} <= set(h)


def test_reindex_after_restart_restores_retrieval(svc, make_token, auth, monkeypatch):
    c, _ = svc
    teacher = make_token(["teacher"])
    _upload(c, teacher, auth, "l.docx", make_docx(), lesson_manifest())
    monkeypatch.setattr(km, "_STORE", InMemoryStore())  # a restart empties the in-memory store
    student = make_token(["student"])
    assert c.post("/v1/knowledge/query", headers=auth(student), json={"query": "helicase"}).json()["results"] == []
    vid = c.get("/v1/knowledge/sources", headers=auth(teacher)).json()["sources"][0]["version_id"]
    assert c.post(f"/v1/knowledge/sources/{vid}/reindex", headers=auth(student)).status_code == 403
    assert c.post(f"/v1/knowledge/sources/{vid}/reindex", headers=auth(teacher)).status_code == 202
    assert c.post("/v1/knowledge/query", headers=auth(student), json={"query": "helicase"}).json()["results"]
    monkeypatch.setattr(km, "_STORE", InMemoryStore())
    assert c.post("/v1/knowledge/sources/all/reindex", headers=auth(teacher)).json() == {"queued": "all"}
    assert c.post("/v1/knowledge/query", headers=auth(student), json={"query": "helicase"}).json()["results"]
