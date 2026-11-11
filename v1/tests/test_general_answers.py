"""General-knowledge answers when the curriculum has no evidence.

A general answer is always labelled, never 'grounded', never carries
curriculum citations, and can be switched off per deployment, per role and
per request. Web answers cite only the results they were given.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from v1.knowledge import config, embeddings, general, grounding
from v1.knowledge import main as km
from v1.knowledge.indexing import IndexManager
from v1.knowledge.ingestion.pipeline import Pipeline
from v1.knowledge.store import InMemoryStore

from .knowledge_fixtures import APPROVED, FakeGateway, text_pdf

QUESTION_OFF = "Who won the rugby league State of Origin last year?"
QUESTION_ON = "How does a covalent bond form between two atoms sharing electrons?"


@pytest.fixture
def svc(monkeypatch, make_token, auth):
    monkeypatch.setattr(embeddings, "_gateway", FakeGateway())
    monkeypatch.setattr(config, "EVIDENCE_THRESHOLDS", {"default": {"min": 0.30, "strong": 0.45}})
    monkeypatch.setattr(config, "GENERAL_ANSWERS", "llm")
    monkeypatch.setattr(config, "GENERAL_ROLES", frozenset({"*"}))
    pipe = Pipeline()
    monkeypatch.setattr(km, "PIPE", pipe)
    monkeypatch.setattr(km, "INDEX", IndexManager(InMemoryStore(), pipe.registry))
    state = {"general_calls": [], "general_reply": "Origin results change every year. [S1] See [3].",
             "curriculum_reply": None, "web": [], "web_error": None, "general_error": None}

    async def curriculum_chat(messages, trace_id=None):
        if state["curriculum_reply"]:
            return state["curriculum_reply"], "nexa-chat-precise"
        import re
        first = re.search(r"\[S1\][^\n]*\n(.+)", messages[1]["content"]).group(1)
        return first[:200].rstrip(".") + ". [S1]", "nexa-chat-precise"

    async def general_chat(messages, trace_id=None):
        state["general_calls"].append(messages)
        if state["general_error"]:
            raise state["general_error"]
        return state["general_reply"], "nexa-general"

    async def web_search(q):
        if state["web_error"]:
            raise state["web_error"]
        return state["web"]

    monkeypatch.setattr(grounding, "chat", curriculum_chat)
    monkeypatch.setattr(general, "chat", general_chat)
    monkeypatch.setattr(general, "web_search", web_search)
    with TestClient(km.app) as c:
        admin = make_token(["platform_admin"], sub="user:admin")
        md = {"source_id": "png-ndoe:chem-guide-gr11", "source_type": "student_guide", "subject": "Chemistry",
              "grade_levels": [11], "title": "Chemistry Student Guide Grade 11", "scope": "national",
              "access_classification": "learner", "publisher": "NDoE PNG",
              "source_uri": "repo://curriculum/chem", **APPROVED}
        data = text_pdf([["Covalent Bonding", "A covalent bond forms when two atoms share a pair of electrons "
                          "between them."]])
        r = c.post("/v1/knowledge/sources", headers=auth(admin), files={"file": ("chem.pdf", data)},
                   data={"manifest": json.dumps({"metadata": md})})
        assert r.status_code == 202, r.text
        student = auth(make_token(["student"], sub="user:student"))
        yield c, student, state, auth, make_token


def _ask(c, h, q, **kw):
    r = c.post("/v1/knowledge/answer", headers=h, json={"question": q, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_off_curriculum_question_gets_a_labelled_general_answer(svc):
    c, h, state, *_ = svc
    a = _ask(c, h, QUESTION_OFF)
    assert a["status"] == "general" and a["source_kind"] == "general_knowledge"
    assert a["answer"].startswith(general.LABEL_LLM)
    assert a["confidence"] == "low" and a["model"] == "nexa-general"
    # never passed off as curriculum: no curriculum citations, invented markers removed and reported
    assert a["citations"] == [] and "[S1]" not in a["answer"] and "[3]" not in a["answer"]
    assert set(a["general"]["fabricated_markers"]) == {"S1", "3"}
    assert "PNG curriculum" in state["general_calls"][0][0]["content"]  # told not to claim curriculum
    assert a["schema_version"] == "1.1.0"


def test_curriculum_answers_are_unchanged(svc):
    c, h, state, *_ = svc
    a = _ask(c, h, QUESTION_ON)
    assert a["status"] == "grounded" and a["source_kind"] == "curriculum" and a["general"] is None
    assert a["citations"] and not state["general_calls"]


def test_passages_that_do_not_answer_also_fall_back_to_general(svc):
    c, h, state, *_ = svc
    state["curriculum_reply"] = "INSUFFICIENT_EVIDENCE"
    a = _ask(c, h, QUESTION_ON)
    assert a["status"] == "general" and a["answer"].startswith(general.LABEL_LLM)
    assert "do not answer" in a["general"]["reason"]


def test_request_deployment_and_role_can_keep_it_strict(svc, monkeypatch):
    c, h, state, auth, make_token = svc
    assert _ask(c, h, QUESTION_OFF, general=False)["status"] == "no_evidence"
    monkeypatch.setattr(config, "GENERAL_ROLES", frozenset({"teacher"}))
    assert _ask(c, h, QUESTION_OFF)["status"] == "no_evidence"
    teacher = auth(make_token(["teacher"], sub="user:teacher"))
    assert _ask(c, teacher, QUESTION_OFF)["status"] == "general"
    monkeypatch.setattr(config, "GENERAL_ANSWERS", "off")
    a = _ask(c, teacher, QUESTION_OFF, general=True)  # a request cannot turn it on
    assert a["status"] == "no_evidence" and a["answer"] == grounding.FALLBACK_NO_EVIDENCE
    assert len(state["general_calls"]) == 1


def test_web_mode_cites_only_the_results_it_was_given(svc, monkeypatch):
    c, h, state, *_ = svc
    monkeypatch.setattr(config, "GENERAL_ANSWERS", "web")
    state["web"] = [{"title": "State of Origin", "url": "https://en.wikipedia.org/wiki/State_of_Origin_series",
                     "snippet": "The State of Origin series is played annually."},
                    {"title": "Rugby league", "url": "https://en.wikipedia.org/wiki/Rugby_league", "snippet": "x"}]
    state["general_reply"] = "It is played every year [W1]. Queensland won [W7]."
    a = _ask(c, h, QUESTION_OFF)
    assert a["status"] == "general" and a["source_kind"] == "web"
    assert a["answer"].startswith(general.LABEL_WEB) and "[W1]" in a["answer"] and "[W7]" not in a["answer"]
    ws = a["general"]["web_sources"]
    assert [w["used"] for w in ws] == [True, False] and ws[0]["url"].startswith("https://")
    assert a["general"]["fabricated_markers"] == ["W7"]
    assert "Web results" in state["general_calls"][0][1]["content"]


def test_web_failure_falls_back_to_the_model_and_model_failure_to_the_refusal(svc, monkeypatch):
    c, h, state, *_ = svc
    monkeypatch.setattr(config, "GENERAL_ANSWERS", "web")
    state["web_error"] = httpx.ConnectError("no route")
    a = _ask(c, h, QUESTION_OFF)
    assert a["status"] == "general" and a["source_kind"] == "general_knowledge"
    assert "web search unavailable" in a["general"]["reason"]
    state["general_error"] = RuntimeError("gateway 503")
    a = _ask(c, h, QUESTION_OFF)
    assert a["status"] == "no_evidence" and any("general answer unavailable" in n for n in a["notes"])


def test_general_model_falls_back_to_the_answer_model(monkeypatch):
    seen = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append(body["model"])
        if body["model"] == "nexa-general":
            return httpx.Response(503, json={"detail": "no OPENAI_API_KEY"})
        return httpx.Response(200, json={"model": body["model"],
                                         "choices": [{"message": {"content": "An answer."}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(general.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    import asyncio
    text, model = asyncio.run(general._general_chat([{"role": "user", "content": "q"}], None))
    assert (text, model) == ("An answer.", "nexa-chat-precise") and seen == ["nexa-general", "nexa-chat-precise"]
