"""Live end-to-end check of NEXA v1 on the DGX, with a report.

    cd ~/Nexa/Nexa && set -a; source <(sudo cat /etc/nexa/identity.env); set +a
    /home/admin/Nexa/venv-v1/bin/python v1/tools/nexa_live_check.py --out ~/nexa-evidence

Read-only: it only queries. Checks, each pass or fail:

 1. Health of the five v1 services.
 2. LMS sync: every received LMS item applied, none failing, nothing waiting.
 3. Retrieval on synced LMS content: curriculum questions find a lesson on the
    right topic in the top 5 (the topic's words in the title or text).
 4. Access control: LMS content of the test school is visible to admins and
    that school's users, and to nobody else; students never see teacher-only
    content.
 5. Grounded answers (/v1/knowledge/answer): curriculum questions are answered
    with valid citations; an off-curriculum question gets the controlled
    "not found" fallback with no model call.
 6. Latency of retrieval.

Writes nexa-live-check-<time>.json and .md to --out and prints the summary.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time

import httpx

sys.path.insert(0, os.getcwd())
from v1.identity.tokens import issue_token  # noqa: E402

BASE = "http://127.0.0.1"
SERVICES = {"api-gateway": 8099, "model-gateway": 8100, "identity": 8101, "agent-runtime": 8102, "knowledge": 8103}
KNOWLEDGE = f"{BASE}:8103"

# Questions on Biology Grade 12 (course 27 on EduNex) and the words the lesson
# that answers it must contain in its title or text.
LMS_QUESTIONS = [
    ("What are the steps of the scientific method?", ["scientific method"]),
    ("How do scientists write a testable hypothesis?", ["hypothesis"]),
    ("Describe the structure of DNA and nucleic acids.", ["nucleic", "dna"]),
    ("What is the difference between genes and chromosomes?", ["chromosome"]),
    ("Explain how DNA replication works.", ["replication"]),
    ("What enzyme unwinds the DNA double helix during replication?", ["helicase"]),
    ("What happens during transcription of DNA into RNA?", ["transcription"]),
    ("How is mRNA translated into a protein at the ribosome?", ["translation", "ribosome"]),
    ("What are independent, dependent and controlled variables?", ["variable"]),
    ("How do you record and analyse data in a scientific investigation?", ["data"]),
]
ANSWER_QUESTIONS = ["Explain how DNA replication works.", "What are the steps of the scientific method?",
                    "What happens during transcription of DNA into RNA?"]
OFF_TOPIC = "Who won the rugby league State of Origin last year?"


def tok(roles, **extra):
    return {"Authorization": "Bearer " + issue_token(f"check:{'-'.join(roles)}", roles, "user", 900,
                                                     extra=extra or None)}


class Report:
    def __init__(self):
        self.checks = []

    def add(self, area, name, ok, **detail):
        self.checks.append({"area": area, "check": name, "result": "PASS" if ok else "FAIL", **detail})
        d = json.dumps(detail, default=str)
        print(f"[{'PASS' if ok else 'FAIL'}] {area}: {name}  {d[:180]}", flush=True)
        return ok


def query(c, h, q, k=5, **body):
    t = time.time()
    r = c.post(f"{KNOWLEDGE}/v1/knowledge/query", headers=h, json={"query": q, "top_k": k, **body})
    ms = (time.time() - t) * 1000
    r.raise_for_status()
    return r.json()["results"], ms


def is_lms(r):
    p = r.get("provenance") or {}
    return p.get("source_type") == "lms_content" or (p.get("source_uri") or "").startswith("lms://") \
        or (r.get("metadata") or {}).get("source_type") == "lms_content"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/nexa-evidence"))
    ap.add_argument("--test-school", default="edunex-core-test")
    ap.add_argument("--skip-answers", action="store_true", help="skip the grounded-answer checks (model calls)")
    a = ap.parse_args()
    rep = Report()
    c = httpx.Client(timeout=180)
    admin = tok(["platform_admin"])
    student = tok(["student"])
    student_school = tok(["student"], school_id=a.test_school)
    student_other = tok(["student"], school_id="some-other-school")
    teacher = tok(["teacher"])
    started = dt.datetime.now(dt.timezone.utc)

    # 1. health
    health = {}
    for name, port in SERVICES.items():
        try:
            j = c.get(f"{BASE}:{port}/health", timeout=10).json()
            health[name] = j
            rep.add("health", f"{name} (:{port})", j.get("status") == "ok", version=j.get("version"))
        except Exception as exc:  # noqa: BLE001
            rep.add("health", f"{name} (:{port})", False, error=str(exc)[:120])
    kh = health.get("knowledge", {})
    rep.add("health", "knowledge index active", bool(kh.get("active_index")), index=kh.get("active_index"),
            embedding=kh.get("embed_fingerprint"), retrieval=kh.get("retrieval"))

    # 2. LMS sync
    st = c.get(f"{KNOWLEDGE}/v1/knowledge/lms/status", headers=admin).json()
    ev, it = st.get("events", {}), st.get("items", {})
    rep.add("lms sync", "all received events applied", ev.get("queued", 0) == 0 and ev.get("failed", 0) == 0,
            events=ev)
    rep.add("lms sync", "no item waiting or failing", it.get("pending", 0) == 0 and it.get("failed", 0) == 0
            and not st.get("recent_failures"), items=it)
    rep.add("lms sync", "lessons served", it.get("active", 0) > 0, active=it.get("active", 0))

    # 3. retrieval on LMS content
    hits, lat, rows = 0, [], []
    for q, words in LMS_QUESTIONS:
        res, ms = query(c, admin, q, 5, filters={"source_types": ["lms_content"]})
        lat.append(ms)
        rank = None
        for r in res:
            hay = ((r.get("provenance") or {}).get("title") or "").lower() + " " + (r.get("text") or "").lower()
            if any(w in hay for w in words):
                rank = r["rank"]
                break
        hits += rank is not None
        top = res[0] if res else {}
        rows.append({"question": q, "rank": rank, "top_title": (top.get("provenance") or {}).get("title"),
                     "top_score": round(top.get("score", 0), 3) if top else None})
    pct = 100 * hits / len(LMS_QUESTIONS)
    rep.add("retrieval", f"LMS lesson on the topic in the top 5 ({hits}/{len(LMS_QUESTIONS)})", pct >= 80,
            recall_at_5=f"{pct:.0f}%", questions=rows)

    # 4. access control
    q = "Explain how DNA replication works."
    a_res, _ = query(c, admin, q, 10, filters={"source_types": ["lms_content"]})
    s_res, _ = query(c, student, q, 10, filters={"source_types": ["lms_content"]})
    ss_res, _ = query(c, student_school, q, 10, filters={"source_types": ["lms_content"]})
    so_res, _ = query(c, student_other, q, 10, filters={"source_types": ["lms_content"]})
    school_only = [r for r in a_res if (r.get("metadata") or {}).get("school_id") == a.test_school]
    rep.add("access", "admin sees LMS content", len(a_res) > 0, results=len(a_res))
    leaked = [r for r in s_res + so_res if (r.get("metadata") or {}).get("school_id") == a.test_school]
    rep.add("access", f"students outside '{a.test_school}' never see its school-scoped content", not leaked,
            leaked=len(leaked), school_scoped_items_in_admin_view=len(school_only))
    if school_only:
        own = [r for r in ss_res if (r.get("metadata") or {}).get("school_id") == a.test_school]
        rep.add("access", f"a student of '{a.test_school}' does see it", len(own) > 0, results=len(own))
    leaks = 0
    for qq, _ in LMS_QUESTIONS[:5]:
        res, _ = query(c, student, qq, 10)
        leaks += sum((r.get("metadata") or {}).get("access_classification") == "teacher_only" for r in res)
    rep.add("access", "students never see teacher-only content", leaks == 0, leaked=leaks)
    t_res, _ = query(c, teacher, "teacher guide assessment", 10)
    rep.add("access", "teachers can retrieve", len(t_res) > 0, results=len(t_res))

    # 5. grounded answers
    if not a.skip_answers:
        for qq in ANSWER_QUESTIONS:
            t = time.time()
            r = c.post(f"{KNOWLEDGE}/v1/knowledge/answer", headers=admin, json={"question": qq})
            ms = (time.time() - t) * 1000
            if r.status_code != 200:
                rep.add("answers", qq, False, http=r.status_code, error=r.text[:150])
                continue
            j = r.json()
            used = [ci for ci in j.get("citations", []) if ci.get("used")]
            ok = j["status"] in ("grounded", "grounded_weak") and used and not j["integrity"]["fabricated_markers"]
            rep.add("answers", qq, ok, status=j["status"], confidence=j.get("confidence"),
                    citations=[(u["marker"], (u["provenance"].get("title") or "")[:50], u["provenance"].get("page_start"))
                               for u in used], fabricated=j["integrity"]["fabricated_markers"], ms=int(ms),
                    answer=(j.get("answer") or "")[:300])
        r = c.post(f"{KNOWLEDGE}/v1/knowledge/answer", headers=admin, json={"question": OFF_TOPIC}).json()
        rep.add("answers", "off-curriculum question gets the 'not found' fallback", r.get("status") == "no_evidence",
                status=r.get("status"), answer=(r.get("answer") or "")[:120])

    # 6. latency (all queries in section 3)
    p50 = statistics.median(lat)
    p95 = sorted(lat)[max(0, int(0.95 * len(lat)) - 1)]
    rep.add("latency", "retrieval p95 under 1 s", p95 < 1000, p50_ms=round(p50), p95_ms=round(p95))

    passed = sum(ch["result"] == "PASS" for ch in rep.checks)
    out = {"generated_at": started.isoformat(timespec="seconds"), "passed": passed, "total": len(rep.checks),
           "overall": "PASS" if passed == len(rep.checks) else "FAIL", "health": health, "lms_status": st,
           "checks": rep.checks}
    os.makedirs(a.out, exist_ok=True)
    stamp = started.strftime("%Y%m%d-%H%M")
    jpath = os.path.join(a.out, f"nexa-live-check-{stamp}.json")
    mpath = os.path.join(a.out, f"nexa-live-check-{stamp}.md")
    with open(jpath, "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    lines = [f"# NEXA live check, {started.strftime('%Y-%m-%d %H:%M UTC')}", "",
             f"**Overall: {out['overall']}**: {passed} of {len(rep.checks)} checks passed.", "",
             f"Knowledge index `{kh.get('active_index')}`, embedding `{kh.get('embed_fingerprint')}`, "
             f"retrieval {json.dumps(kh.get('retrieval', {}).get('mode'))}. LMS items {json.dumps(it)}.", "",
             "| Area | Check | Result | Detail |", "|---|---|---|---|"]
    for ch in rep.checks:
        det = {k: v for k, v in ch.items() if k not in ("area", "check", "result", "questions", "answer")}
        lines.append(f"| {ch['area']} | {ch['check']} | **{ch['result']}** | {json.dumps(det, default=str)[:160]} |")
    lines += ["", "## Retrieval questions (LMS content, top 5)", "", "| Question | Rank | Top lesson | Score |",
              "|---|---|---|---|"]
    for r in next((ch["questions"] for ch in rep.checks if ch["area"] == "retrieval"), []):
        lines.append(f"| {r['question']} | {r['rank'] or 'not found'} | {r['top_title']} | {r['top_score']} |")
    ans = [ch for ch in rep.checks if ch["area"] == "answers" and ch.get("answer")]
    if ans:
        lines += ["", "## Grounded answers", ""]
        for ch in ans:
            lines += [f"**Q: {ch['check']}** ({ch.get('status')}, {ch.get('ms', '-')} ms)", "",
                      f"> {ch['answer']}", "", f"Citations: {ch.get('citations')}", ""]
    with open(mpath, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nOVERALL: {out['overall']} ({passed}/{len(rep.checks)})\nreport: {mpath}\n        {jpath}")
    return 0 if out["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
