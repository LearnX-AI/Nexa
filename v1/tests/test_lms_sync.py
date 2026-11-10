"""NEXA-M2-007: LMS knowledge synchronisation (contract nexa.lms-sync/1.0).

Drives the real HTTP endpoints with a deterministic embedder, the way a
Moodle site's sync client would: upload content by hash, push events,
reconcile, retry.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import itertools

import pytest
from fastapi.testclient import TestClient

from v1.identity import tokens
from v1.knowledge import embeddings
from v1.knowledge import main as km
from v1.knowledge.indexing import IndexManager
from v1.knowledge.ingestion.pipeline import Pipeline
from v1.knowledge.store import InMemoryStore

from .knowledge_fixtures import FakeGateway, make_docx

SITE = {"system": "moodle", "site_id": "edunex-ncd", "school_id": "pg-ncd-0042"}
_ids = itertools.count(1)
_t0 = dt.datetime(2026, 11, 1, 8, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def svc(monkeypatch):
    gw = FakeGateway()
    monkeypatch.setattr(embeddings, "_gateway", gw)
    pipe = Pipeline()
    monkeypatch.setattr(km, "PIPE", pipe)
    monkeypatch.setattr(km, "INDEX", IndexManager(InMemoryStore(), pipe.registry))
    with TestClient(km.app) as c:
        yield c, gw


def sync_token(site="edunex-ncd", school="pg-ncd-0042", national=False):
    extra = {"lms_site": site}
    if school:
        extra["school_id"] = school
    if national:
        extra["lms_national"] = True
    return tokens.issue_token(f"lms:{site}", ["lms_sync"], "service", 300, extra=extra)


def user(roles, school=None):
    return tokens.issue_token("user:x", roles, "user", 300, extra={"school_id": school} if school else None)


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def event(item_id="cm100", op="upsert", minutes=0, body=None, file=None, **item):
    it = {"course_id": "bio12", "item_id": item_id, "course_name": "Grade 12 Biology", "kind": "page",
          "revision": "1", "title": "Photosynthesis notes", "visibility": "students",
          "approval": {"status": "approved", "by": "moodle-user:12"},
          "curriculum": {"subject": "Biology", "grade_levels": [12]}}
    if op == "upsert":
        it["content"] = {"file": file} if file else {"format": "html", "body": body or (
            "<h2>Photosynthesis</h2><p>Chloroplasts capture light energy and convert carbon dioxide and water "
            "into glucose and oxygen.</p>")}
    else:
        it = {"course_id": "bio12", "item_id": item_id}
    it.update(item)
    return {"event_id": f"evt-{next(_ids):06d}-abcdef", "op": op,
            "occurred_at": (_t0 + dt.timedelta(minutes=minutes)).isoformat(), "item": it}


def push(c, events, tok=None, wait=True, site=None):
    return c.post(f"/v1/knowledge/lms/events?wait={'true' if wait else 'false'}", headers=H(tok or sync_token()),
                  json={"contract": "nexa.lms-sync/1.0", "site": site or SITE, "events": events})


def ask(c, tok, q="how do chloroplasts make glucose from light energy"):
    r = c.post("/v1/knowledge/query", headers=H(tok), json={"query": q, "top_k": 10})
    assert r.status_code == 200, r.text
    return r.json()["results"]


def lms_hits(results, item_id="cm100"):
    return [r for r in results if r["provenance"].get("lms", {}).get("item_id") == item_id]


# ---------------------------------------------------------------- auth and contract

def test_token_is_bound_to_its_site_and_school(svc):
    c, _ = svc
    assert push(c, [event()], tok=sync_token(site="other-site")).status_code == 403
    bad_school = dict(SITE, school_id="pg-other-0001")
    assert push(c, [event()], site=bad_school).status_code == 403
    assert push(c, [event()], tok=user(["student"])).status_code == 403
    # A sync token cannot query or upload directly.
    assert c.post("/v1/knowledge/query", headers=H(sync_token()), json={"query": "x"}).status_code == 403
    r = push(c, [event(scope="national")])
    assert r.json()["results"][0]["status"] == "rejected" and "lms_national" in r.json()["results"][0]["reason"]
    r = push(c, [event(scope="national")], tok=sync_token(national=True))
    assert r.json()["results"][0]["state"] == "applied"
    no_school = push(c, [event("cm101")], tok=sync_token(school=None), site={**SITE, "school_id": None})
    assert no_school.json()["results"][0]["status"] == "rejected"


def test_contract_validation(svc):
    c, _ = svc
    r = c.post("/v1/knowledge/lms/events", headers=H(sync_token()),
               json={"contract": "nexa.lms-sync/2.0", "site": SITE, "events": [event()]})
    assert r.status_code == 422
    e = event()
    e["item"]["url"] = "https://moodle/mod/page/view.php?id=4&sesskey=abc"
    assert push(c, [e]).status_code == 422
    e = event()
    del e["item"]["approval"]
    assert push(c, [e]).status_code == 422
    r = push(c, [event(curriculum={"subject": "Biology", "grade_levels": []})])
    assert r.json()["results"][0]["status"] == "rejected" and "grade_levels" in r.json()["results"][0]["reason"]
    schema = c.get("/v1/knowledge/lms/schema").json()
    assert schema["$id"].endswith("/1.0.0.json") and "events_request" in schema


# ---------------------------------------------------------------- end to end

def test_approved_lms_page_becomes_retrievable_for_its_school_only(svc):
    c, _ = svc
    r = push(c, [event()]).json()
    assert r["results"][0]["status"] == "accepted" and r["results"][0]["state"] == "applied"
    own = lms_hits(ask(c, user(["student"], school="pg-ncd-0042")))
    assert own and own[0]["provenance"]["source_id"] == "lms:edunex-ncd:cm100"
    assert own[0]["metadata"]["scope"] == "school" and own[0]["metadata"]["source_type"] == "lms_content"
    assert own[0]["provenance"]["source_uri"].startswith("lms://edunex-ncd/course/bio12/item/cm100")
    assert not lms_hits(ask(c, user(["student"], school="pg-other-0001")))
    assert not lms_hits(ask(c, user(["student"])))  # no school claim
    assert lms_hits(ask(c, user(["platform_admin"])))
    item = km.PIPE.registry.get_lms_item("moodle:edunex-ncd:cm100")
    assert item["state"] == "active" and item["revision"] == "1"


def test_file_content_by_hash_and_missing_content_retry(svc):
    c, _ = svc
    data = make_docx()
    digest = hashlib.sha256(data).hexdigest()
    f = {"sha256": digest, "name": "dna.docx", "size": len(data)}
    r = push(c, [event("cm200", file=f, title="DNA replication")]).json()["results"][0]
    assert r["state"] == "failed" and "not been uploaded" in r["error"]
    status = c.get("/v1/knowledge/lms/status", headers=H(sync_token())).json()
    assert status["events"]["failed"] == 1 and status["recent_failures"][0]["item_key"].endswith(":cm200")
    assert c.head(f"/v1/knowledge/lms/content/{digest}", headers=H(sync_token())).status_code == 404
    assert c.put(f"/v1/knowledge/lms/content/{'0' * 64}", headers=H(sync_token()), content=data).status_code == 422
    up = c.put(f"/v1/knowledge/lms/content/{digest}", headers=H(sync_token()), content=data).json()
    assert up["stored"] is True
    assert c.head(f"/v1/knowledge/lms/content/{digest}", headers=H(sync_token())).status_code == 200
    again = c.post(f"/v1/knowledge/lms/events/{r['event_id']}/retry", headers=H(sync_token())).json()
    assert again["state"] == "applied" and again["attempts"] == 2
    hits = lms_hits(ask(c, user(["student"], school="pg-ncd-0042"), "which enzyme unwinds the DNA"), "cm200")
    assert hits and "Helicase" in " ".join(h["text"] for h in hits)


def test_changed_superseded_pending_hidden_and_deleted(svc):
    c, _ = svc
    stu, tea = user(["student"], school="pg-ncd-0042"), user(["teacher"], school="pg-ncd-0042")
    reg = km.PIPE.registry
    push(c, [event(minutes=0)])
    v1 = reg.get_lms_item("moodle:edunex-ncd:cm100")["version_id"]
    # changed content -> new version supersedes the old one
    body2 = "<p>Chloroplasts capture light energy; the Calvin cycle then fixes carbon dioxide into glucose.</p>"
    push(c, [event(minutes=5, revision="2", body=body2)])
    v2 = reg.get_lms_item("moodle:edunex-ncd:cm100")["version_id"]
    assert v2 != v1
    versions = {v["version_id"]: v["status"] for v in reg.versions_of("lms:edunex-ncd:cm100")}
    assert versions == {v1: "superseded", v2: "active"}  # history kept
    texts = " ".join(h["text"] for h in lms_hits(ask(c, stu)))
    assert "Calvin cycle" in texts and "into glucose and oxygen" not in texts
    # a pending revision does not replace the approved one
    body3 = "<p>DRAFT: chloroplasts and light energy, rewritten.</p>"
    push(c, [event(minutes=10, revision="3", body=body3, approval={"status": "pending_review"})])
    it = reg.get_lms_item("moodle:edunex-ncd:cm100")
    assert it["state"] == "active" and "pending_review" in it["detail"]
    texts = " ".join(h["text"] for h in lms_hits(ask(c, stu)))
    assert "Calvin cycle" in texts and "DRAFT" not in texts
    # hidden from students -> teacher only
    push(c, [event(minutes=15, revision="2", body=body2, visibility="hidden")])
    assert not lms_hits(ask(c, stu)) and lms_hits(ask(c, tea))
    # deleted -> gone, history kept, delete is idempotent
    push(c, [event(minutes=20, op="delete")])
    assert not lms_hits(ask(c, tea)) and reg.get_lms_item("moodle:edunex-ncd:cm100")["state"] == "withdrawn"
    assert {v["status"] for v in reg.versions_of("lms:edunex-ncd:cm100")} == {"withdrawn"}
    assert push(c, [event(minutes=21, op="delete")]).json()["results"][0]["state"] == "applied"
    # comes back after deletion (same content as before): reactivated, served again
    push(c, [event(minutes=30, revision="2", body=body2)])
    assert lms_hits(ask(c, stu)) and reg.get_lms_item("moodle:edunex-ncd:cm100")["state"] == "active"


def test_approval_revoked_with_same_content_removes_it(svc):
    c, _ = svc
    stu = user(["student"], school="pg-ncd-0042")
    push(c, [event(minutes=0)])
    assert lms_hits(ask(c, stu))
    push(c, [event(minutes=1, approval={"status": "draft"})])
    assert not lms_hits(ask(c, stu))
    assert km.PIPE.registry.get_lms_item("moodle:edunex-ncd:cm100")["state"] == "pending"
    push(c, [event(minutes=2, approval={"status": "withdrawn"})])
    assert km.PIPE.registry.get_lms_item("moodle:edunex-ncd:cm100")["state"] == "withdrawn"


def test_item_superseding_another_item(svc):
    c, _ = svc
    stu = user(["student"], school="pg-ncd-0042")
    push(c, [event("cm300", minutes=0)])
    push(c, [event("cm301", minutes=1, supersedes="cm300",
                   body="<p>New page: chloroplasts turn light energy into glucose via photosynthesis.</p>")])
    hits = ask(c, stu)
    assert not lms_hits(hits, "cm300") and lms_hits(hits, "cm301")
    old = km.PIPE.registry.get_lms_item("moodle:edunex-ncd:cm300")
    assert old["state"] == "withdrawn" and "superseded by" in old["detail"]


def test_stale_and_duplicate_events(svc):
    c, _ = svc
    reg = km.PIPE.registry
    newer = event(minutes=10, revision="5", body="<p>Revision five about chloroplasts and light energy.</p>")
    older = event(minutes=1, revision="4", body="<p>Revision four about chloroplasts.</p>")
    push(c, [newer])
    r = push(c, [older]).json()["results"][0]
    assert r["state"] == "stale"
    assert reg.get_lms_item("moodle:edunex-ncd:cm100")["revision"] == "5"
    dup = push(c, [newer]).json()["results"][0]
    assert dup["status"] == "duplicate" and dup["state"] == "applied"
    reused = dict(newer, item={**newer["item"], "item_id": "cm999"})
    assert push(c, [reused]).json()["results"][0]["status"] == "rejected"
    # out of order inside one batch: applied in occurred_at order
    a, b = event("cm400", minutes=50, revision="2", body="<p>Second chloroplast text.</p>"), \
        event("cm400", minutes=40, revision="1", body="<p>First chloroplast text.</p>")
    res = push(c, [a, b]).json()["results"]
    assert [x["state"] for x in res] == ["applied", "applied"]
    assert reg.get_lms_item("moodle:edunex-ncd:cm400")["revision"] == "2"


def test_gateway_outage_fails_retryably_then_recovers(svc):
    c, gw = svc
    gw.fail = True
    r = push(c, [event()]).json()["results"][0]
    assert r["state"] == "failed"
    ev = c.get(f"/v1/knowledge/lms/events/{r['event_id']}", headers=H(sync_token())).json()
    assert ev["retryable"] and ev["error_code"] == "EmbeddingError"
    listing = c.get("/v1/knowledge/lms/events?state=failed", headers=H(user(["ai_engineer"]))).json()["events"]
    assert [e["event_id"] for e in listing] == [r["event_id"]]
    gw.fail = False
    done = c.post("/v1/knowledge/lms/events/retry-failed", headers=H(user(["ai_engineer"]))).json()
    assert done == {"retried": 1, "applied": 1, "failed": 0}
    assert lms_hits(ask(c, user(["student"], school="pg-ncd-0042")))
    assert c.post("/v1/knowledge/lms/events/retry-failed", headers=H(sync_token())).status_code == 403
    # another site's sync token cannot see this site's events
    other = sync_token(site="other-site", school="pg-x")
    assert c.get(f"/v1/knowledge/lms/events/{r['event_id']}", headers=H(other)).status_code == 404


def test_background_apply(svc):
    c, _ = svc
    r = push(c, [event()], wait=False).json()["results"][0]
    assert r["status"] == "accepted" and "state" not in r
    # TestClient runs background tasks before returning
    assert km.PIPE.registry.get_lms_event(r["event_id"])["state"] == "applied"


def test_reconcile_finds_missing_mismatched_and_deleted_items(svc):
    c, _ = svc
    push(c, [event("cm1", minutes=0), event("cm2", minutes=0, revision="7"), event("cm3", minutes=0)])
    body = {"contract": "nexa.lms-sync/1.0", "site": SITE, "course_id": "bio12",
            "items": [{"item_id": "cm1", "revision": "1"}, {"item_id": "cm2", "revision": "8"},
                      {"item_id": "cm9", "revision": "1"}]}
    r = c.post("/v1/knowledge/lms/reconcile", headers=H(sync_token()), json=body).json()
    assert r["missing_in_nexa"] == ["cm9"] and r["revision_mismatch"] == ["cm2"] and r["not_in_lms"] == ["cm3"]
    assert r["withdrawn"] == [] and not r["in_sync"]
    r = c.post("/v1/knowledge/lms/reconcile", headers=H(sync_token()), json={**body, "apply": True}).json()
    assert r["withdrawn"] == ["cm3"]
    assert km.PIPE.registry.get_lms_item("moodle:edunex-ncd:cm3")["state"] == "withdrawn"
    assert not lms_hits(ask(c, user(["platform_admin"])), "cm3")
    assert c.post("/v1/knowledge/lms/reconcile", headers=H(sync_token(site="x-site")), json=body).status_code == 403


def test_extra_claims_are_limited():
    with pytest.raises(tokens.TokenError):
        tokens.issue_token("x", ["student"], extra={"roles": ["admin"]})
    with pytest.raises(tokens.TokenError):
        tokens.issue_token("x", ["student"], extra={"favourite_colour": "blue"})
    claims = tokens.verify_token(tokens.issue_token("x", ["lms_sync"], extra={"lms_site": "s1", "school_id": "k"}))
    assert claims["lms_site"] == "s1" and claims["school_id"] == "k"
