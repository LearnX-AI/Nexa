"""NEXA-M2-007 end-to-end demonstration against a running Knowledge Service.

    set -a; source <(sudo cat /etc/nexa/identity.env); set +a
    python v1/tools/lms_sync_demo.py --base http://127.0.0.1:8103 --out docs/knowledge/evidence

Acts as an LMS site's sync client, using only the public contract
(nexa.lms-sync/1.0), and records a pass or fail for every step:

 1. An approved LMS page and an approved LMS file (uploaded by hash) become
    retrievable for students of the site's school, with LMS provenance.
 2. A student of another school, and a student with no school, cannot see them.
 3. A changed revision supersedes the old one; the old text is no longer served.
 4. A pending revision does not replace the approved one.
 5. Hidden in the LMS: teachers still see it, students do not.
 6. A failed event (file not uploaded yet) is visible in the event log and
    succeeds on retry once the file is there.
 7. A stale (older) event is ignored; a resent event is a duplicate.
 8. Reconcile finds an item deleted while "offline" and withdraws it.
 9. Delete: gone from retrieval, history kept.

It writes only to its own demo site (lms_site nexa-demo, school
nexa-demo-school) and withdraws everything it created at the end; the
withdrawn versions stay in the registry as history, as for any LMS item.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import sys
import time
import uuid

import httpx

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from v1.identity.tokens import issue_token  # noqa: E402

SITE = {"system": "moodle", "site_id": "nexa-demo", "school_id": "nexa-demo-school"}
RUN = uuid.uuid4().hex[:6]


def tok(roles, **extra):
    return {"Authorization": "Bearer " + issue_token(f"demo:{'-'.join(roles)}", roles,
                                                     "service" if "lms_sync" in roles else "user", 1800,
                                                     extra=extra or None)}


SYNC = None
_n = [0]


def ev(item_id, op="upsert", body=None, file=None, minutes=0, **item):
    _n[0] += 1
    t = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=minutes)
    it = {"course_id": "demo-bio12", "item_id": item_id, "course_name": "Demo Grade 12 Biology",
          "kind": "page", "revision": "1", "title": f"Demo {item_id}", "visibility": "students",
          "approval": {"status": "approved", "by": "moodle-user:demo"},
          "curriculum": {"subject": "Biology", "grade_levels": [12]}}
    if op == "upsert":
        it["content"] = {"file": file} if file else {"format": "html", "body": body}
    else:
        it = {"course_id": "demo-bio12", "item_id": item_id}
    it.update(item)
    return {"event_id": f"demo-{RUN}-{_n[0]:04d}", "op": op, "occurred_at": t.isoformat(), "item": it}


class Demo:
    def __init__(self, base):
        self.c = httpx.Client(base_url=base, timeout=300)
        self.steps = []
        self.t0 = time.time()

    def step(self, name, ok, **detail):
        ok = bool(ok)
        self.steps.append({"step": len(self.steps) + 1, "name": name, "result": "pass" if ok else "FAIL",
                           "t_seconds": round(time.time() - self.t0, 1), **detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {json.dumps(detail, default=str)[:220]}", flush=True)

    def push(self, events):
        r = self.c.post("/v1/knowledge/lms/events?wait=true", headers=SYNC,
                        json={"contract": "nexa.lms-sync/1.0", "site": SITE, "events": events})
        r.raise_for_status()
        return r.json()["results"]

    def find(self, headers, q, item):
        r = self.c.post("/v1/knowledge/query", headers=headers, json={"query": q, "top_k": 10})
        r.raise_for_status()
        return [x for x in r.json()["results"] if (x.get("provenance") or {}).get("lms", {}).get("item_id") == item]


def docx_bytes(text):
    import docx
    d = docx.Document()
    d.add_heading("Demo cell respiration", level=1)
    d.add_paragraph(text)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def main() -> int:
    global SYNC
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8103")
    ap.add_argument("--out", default=".")
    a = ap.parse_args()
    SYNC = tok(["lms_sync"], lms_site=SITE["site_id"], school_id=SITE["school_id"])
    stu = tok(["student"], school_id=SITE["school_id"])
    other = tok(["student"], school_id="another-school")
    nosch = tok(["student"])
    tea = tok(["teacher"], school_id=SITE["school_id"])
    admin = tok(["ai_engineer"])
    d = Demo(a.base)
    page, f_item, gone = f"pg{RUN}", f"fl{RUN}", f"rc{RUN}"
    q_page = f"demo {RUN} how do mangrove roots breathe in waterlogged mud"
    p1 = (f"<h2>Mangroves {RUN}</h2><p>Mangrove roots called pneumatophores grow upward out of the waterlogged "
          f"mud so the tree can take in oxygen from the air. Marker {RUN}-one.</p>")
    try:
        # 1
        r = d.push([ev(page, body=p1)])
        hits = d.find(stu, q_page, page)
        d.step("approved LMS page becomes retrievable for its school", r[0].get("state") == "applied" and hits,
               state=r[0].get("state"), source_id=hits[0]["provenance"]["source_id"] if hits else None,
               source_uri=hits[0]["provenance"].get("source_uri") if hits else None)
        data = docx_bytes(f"Cellular respiration releases energy from glucose in the mitochondria. Marker {RUN}-f.")
        digest = hashlib.sha256(data).hexdigest()
        fref = {"sha256": digest, "name": f"respiration-{RUN}.docx", "size": len(data)}
        # 6 (first half): file not uploaded yet -> failed, observable
        r = d.push([ev(f_item, file=fref, kind="resource", minutes=1)])
        failed_id = r[0]["event_id"]
        st = d.c.get("/v1/knowledge/lms/status", headers=SYNC).json()
        d.step("event for a file not yet uploaded fails and is visible", r[0].get("state") == "failed"
               and any(f["event_id"] == failed_id for f in st["recent_failures"]), error=r[0].get("error"))
        up = d.c.put(f"/v1/knowledge/lms/content/{digest}", headers=SYNC, content=data).json()
        rt = d.c.post(f"/v1/knowledge/lms/events/{failed_id}/retry", headers=SYNC).json()
        fh = d.find(stu, f"demo {RUN} where does cellular respiration release energy from glucose", f_item)
        d.step("after upload by hash, retry succeeds and the file is retrievable", rt["state"] == "applied" and fh,
               uploaded=up, attempts=rt["attempts"])
        # 2
        d.step("other schools and users without a school cannot see school content",
               not d.find(other, q_page, page) and not d.find(nosch, q_page, page))
        # 3
        p2 = p1.replace(f"Marker {RUN}-one", f"Marker {RUN}-two. Revised: lenticels on the roots also let air in")
        d.push([ev(page, body=p2, revision="2", minutes=10)])
        txt = " ".join(h["text"] for h in d.find(stu, q_page, page))
        d.step("changed revision supersedes the old one", f"{RUN}-two" in txt and f"{RUN}-one" not in txt)
        # 4
        p3 = p1.replace(f"Marker {RUN}-one", f"Marker {RUN}-draft")
        r = d.push([ev(page, body=p3, revision="3", minutes=20, approval={"status": "pending_review"})])
        txt = " ".join(h["text"] for h in d.find(stu, q_page, page))
        d.step("pending revision does not replace the approved one", f"{RUN}-two" in txt and "draft" not in txt,
               state=r[0].get("state"))
        # 5
        d.push([ev(page, body=p2, revision="2", minutes=30, visibility="hidden")])
        d.step("hidden in the LMS: teachers see it, students do not",
               not d.find(stu, q_page, page) and d.find(tea, q_page, page))
        visible_again = ev(page, body=p2, revision="2", minutes=31)
        d.push([visible_again])
        # 7
        r = d.push([ev(page, body=p1, revision="1", minutes=5)])
        dup = d.push([visible_again])  # the LMS resends an event it already sent
        txt = " ".join(h["text"] for h in d.find(stu, q_page, page))
        d.step("stale event ignored, resent event is a duplicate", r[0].get("state") == "stale"
               and dup[0]["status"] == "duplicate" and f"{RUN}-two" in txt,
               stale=r[0].get("state"), resend=dup[0]["status"])
        # 8
        d.push([ev(gone, body=f"<p>Demo {RUN} item about coral bleaching.</p>", minutes=40)])
        rec = d.c.post("/v1/knowledge/lms/reconcile", headers=SYNC, json={
            "contract": "nexa.lms-sync/1.0", "site": SITE, "course_id": "demo-bio12", "apply": True,
            "items": [{"item_id": page, "revision": "2"}, {"item_id": f_item, "revision": "1"}]}).json()
        d.step("reconcile withdraws an item deleted while offline", rec.get("withdrawn") == [gone], report=rec)
        # 9
        d.push([ev(page, op="delete", minutes=50)])
        src = d.c.get("/v1/knowledge/lms/events?limit=200", headers=admin).json()["events"]
        d.step("deleted item is gone from retrieval; its events stay in the log",
               not d.find(tea, q_page, page) and any(e["item_key"].endswith(page) for e in src))
    finally:
        d.push([ev(i, op="delete", minutes=99) for i in (page, f_item, gone)])
    report = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "base": a.base,
              "site": SITE, "run": RUN, "passed": all(s["result"] == "pass" for s in d.steps), "steps": d.steps}
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "m2-007-lms-sync-demo.json"), "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"demo {'PASSED' if report['passed'] else 'FAILED'}: {len(d.steps)} steps")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
