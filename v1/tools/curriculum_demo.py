"""NEXA-M4-004 curriculum demo pack: evidence for the digitization team and
the CDD reviewers before any Curriculum Intelligence change is made.

Read-only. It asks the running Knowledge Service for grounded answers, as a
teacher at the pilot school, and records for each case what NEXA answered,
from which sources (title, type, grade, subject, version, checksum) and
whether the automatic check passed. It never ingests, changes or publishes
anything.

The cases:

- covered: two questions for each of the six indexed courses (Grade 11-12
  Biology, Chemistry, Physics): a grounded answer citing the right subject and
  grade is expected;
- not_covered: Grades 7-10 and other subjects, which NEXA does not have yet:
  no curriculum answer is expected, and an answer grounded on another grade
  or subject is reported as a silent substitution;
- no_answer: questions the curriculum cannot answer: a refusal is expected;
- incorrect: questions with a false premise: the answer must correct it (the
  automatic check looks for the correct terms; the reviewer decides);
- source/version: every cited source must be an active source version with a
  content checksum.

The report has a sign-off sheet: the CDD reviewers mark each answer
academically correct or not, and the digitization representative signs off
source integrity.

    cd /home/admin/Nexa/Nexa
    (set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \\
     /home/admin/Nexa/venv310/bin/python -m v1.tools.curriculum_demo --out ~/nexa-evidence/m4-004/demo)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Callable, Optional

GROUNDED = ("grounded", "grounded_weak")

# (id, category, subject, grade, question, expected terms)
CASES: list[tuple[str, str, Optional[str], Optional[int], str, list[str]]] = [
    ("c01", "covered", "Biology", 11, "What did Mendel's experiments with pea plants show about how traits are inherited?",
     ["dominant", "recessive", "allele"]),
    ("c02", "covered", "Biology", 11, "How is recombinant DNA made using restriction enzymes and plasmids?",
     ["restriction", "plasmid", "ligase"]),
    ("c03", "covered", "Biology", 12, "Describe the structure of DNA and how its bases pair.",
     ["double helix", "adenine", "thymine", "guanine", "cytosine"]),
    ("c04", "covered", "Biology", 12, "What is homeostasis and how does negative feedback maintain it?",
     ["homeostasis", "negative feedback"]),
    ("c05", "covered", "Chemistry", 11, "What is the difference between a strong acid and a weak acid?",
     ["ionise", "strong", "weak"]),
    ("c06", "covered", "Chemistry", 11, "Which factors affect the rate of a chemical reaction?",
     ["temperature", "concentration", "surface area", "catalyst"]),
    ("c07", "covered", "Chemistry", 12, "What is the general structure of an amino acid?",
     ["amino group", "carboxyl", "r group"]),
    ("c08", "covered", "Chemistry", 12, "What are the advantages and challenges of using hydrogen as a fuel?",
     ["hydrogen", "water", "storage"]),
    ("c09", "covered", "Physics", 11, "What is the difference between base quantities and derived quantities in physics?",
     ["base", "derived", "si"]),
    ("c10", "covered", "Physics", 11, "How does a computerized tomography (CT) scan produce an image of the body?",
     ["x-ray", "cross-section", "computer"]),
    ("c11", "covered", "Physics", 12, "What is the difference between transverse and longitudinal waves?",
     ["transverse", "longitudinal", "perpendicular", "parallel"]),
    ("c12", "covered", "Physics", 12, "State the first law of thermodynamics.",
     ["energy", "heat", "work"]),
    ("n01", "not_covered", "English", 7, "In Grade 7 English, what is the difference between a noun and a pronoun?", []),
    ("n02", "not_covered", "Social Science", 8, "In Grade 8 Social Science, what are the main landforms of Papua New Guinea?", []),
    ("n03", "not_covered", "Mathematics", 9, "In Grade 9 Mathematics, how do you solve the linear equation 3x + 5 = 20?", []),
    ("n04", "not_covered", "Science", 10, "In Grade 10 Science, what are the parts of a flower and their functions?", []),
    ("n05", "not_covered", "Business Studies", 11, "In Grade 11 Business Studies, what is a balance sheet?", []),
    ("n06", "not_covered", "History", 12, "In Grade 12 History, what led to Papua New Guinea's independence in 1975?", []),
    ("u01", "no_answer", None, None, "Who won the most recent Papua New Guinea general election?", []),
    ("u02", "no_answer", None, None, "What is the price of a 10 kg bag of rice at Boroko market today?", []),
    ("u03", "no_answer", None, None, "What half-life did the 2025 UPNG experiment measure for seaborgium-269?", []),
    ("u04", "no_answer", None, None, "What marks did the Grade 12 Biology class at POMNSOE get in Term 2?", []),
    ("u05", "no_answer", None, None, "Which page of the Grade 12 Physics syllabus covers quantum gravity?", []),
    ("w01", "incorrect", "Biology", 11, "Why do plants take in oxygen and give out carbon dioxide during photosynthesis?",
     ["carbon dioxide", "oxygen", "glucose"]),
    ("w02", "incorrect", "Biology", 12, "Why is the mitochondrion the site of photosynthesis in plant cells?",
     ["chloroplast"]),
    ("w03", "incorrect", "Chemistry", 11, "Why does a strong acid have a pH greater than 7?",
     ["less than 7", "below 7", "lower"]),
    ("w04", "incorrect", "Physics", 12, "Why does sound travel faster in a vacuum than in air?",
     ["medium", "cannot", "vacuum"]),
    ("w05", "incorrect", "Chemistry", 12, "Why are amino acids joined together by hydrogen bonds to form a protein chain?",
     ["peptide"]),
]


def _norm_subject(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def source_table(sources: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {s["version_id"]: s for s in sources if s.get("version_id")}


def judge(case: tuple, body: dict[str, Any], srcs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The automatic check for one case. The reviewer has the final word on
    academic correctness."""
    cid, cat, subject, grade, question, terms = case
    status = body.get("status")
    kind = body.get("source_kind")
    answer = body.get("answer") or ""
    cited = [c for c in body.get("citations", []) if c.get("used")]
    cited_src = []
    for c in cited:
        p = c.get("provenance") or {}
        s = srcs.get(p.get("version_id") or "")
        cited_src.append({
            "marker": c.get("marker"), "title": p.get("title") or (s or {}).get("title"),
            "source_type": p.get("source_type") or (s or {}).get("source_type"),
            "subject": (s or {}).get("subject"), "grades": (s or {}).get("grade_levels"),
            "version_id": p.get("version_id"), "checksum": bool(p.get("content_checksum")),
            "active": s is not None, "citation": p.get("citation")})
    curriculum = status in GROUNDED and kind == "curriculum"
    low = answer.lower()
    have = [t for t in terms if t in low]
    fabricated = len((body.get("integrity") or {}).get("fabricated_markers") or [])
    issues: list[str] = []
    if fabricated:
        issues.append(f"{fabricated} fabricated citation marker(s)")
    mapping = None
    if cat in ("covered", "incorrect") and curriculum:
        subj = [x for x in cited_src if _norm_subject(x["subject"]) not in ("", _norm_subject(subject))]
        other = [x for x in cited_src if x not in subj and x["grades"] and grade not in x["grades"]]
        mapping = "wrong_subject" if subj else ("other_grade" if other else "right")
        if subj:
            issues.append("cites another subject: " + "; ".join(
                f"{x['title']} ({x['subject']} {x['grades']})" for x in subj))
        if other:
            issues.append("cites another grade of the same subject: " + "; ".join(
                f"{x['title']} ({x['subject']} {x['grades']})" for x in other))
    if cat == "covered":
        if not curriculum:
            issues.append(f"not grounded ({status})")
        elif terms and len(have) < max(1, len(terms) // 2):
            issues.append(f"key terms {len(have)}/{len(terms)}")
    elif cat in ("not_covered", "no_answer"):
        if curriculum:
            issues.append("answered from the curriculum: silent substitution from " + "; ".join(
                f"{x['title']} ({x['subject']} {x['grades']})" for x in cited_src))
    elif cat == "incorrect":
        if not curriculum:
            issues.append(f"refused ({status}) instead of correcting the premise from the curriculum")
        elif terms and not have:
            issues.append("the false premise may not be corrected (no correct term found)")
    if curriculum:
        if any(not x["active"] for x in cited_src):
            issues.append("cites a source version that is not active")
        if any(not x["checksum"] for x in cited_src):
            issues.append("a cited source has no content checksum")
    return {"id": cid, "category": cat, "subject": subject, "grade": grade, "question": question,
            "status": status, "source_kind": kind, "confidence": body.get("confidence"),
            "answer": answer, "sources": cited_src, "terms_found": have, "terms": terms,
            "fabricated": fabricated, "mapping": mapping, "passed": not issues, "issues": issues}


def run(post: Callable[..., dict[str, Any]], sources: list[dict[str, Any]],
        cases: Optional[list[tuple]] = None, progress: Callable[[str], None] = lambda s: None,
        filtered: bool = True) -> dict[str, Any]:
    """Each case is asked as a user would type it. With filtered=True, covered
    and incorrect-premise cases are asked a second time with their grade and
    subject as retrieval filters: what an agent that knows the learner's grade
    and subject would get (ids ending in "f")."""
    srcs = source_table(sources)
    results, lat = [], []
    plan = []
    for case in cases or CASES:
        plan.append((case, None))
        if filtered and case[1] in ("covered", "incorrect") and case[2] and case[3]:
            plan.append((case, {"subjects": [case[2]], "grades": [case[3]]}))
    for case, flt in plan:
        t0 = time.time()
        try:
            body = post(case[4], flt)
        except Exception as exc:  # noqa: BLE001 - recorded as a failed case
            body = {"status": f"error: {type(exc).__name__}", "citations": []}
        lat.append(time.time() - t0)
        r = judge(case, body, srcs)
        if flt:
            r["id"] += "f"
            r["filters"] = flt
        r["seconds"] = round(lat[-1], 1)
        results.append(r)
        progress(f"  {'ok  ' if r['passed'] else 'MISS'} {r['id']:<4} {r['category']:<11} {str(r['status']):<15} "
                 + ("; ".join(r["issues"]) if r["issues"] else ""))
    cats: dict[str, dict[str, int]] = {}
    for r in results:
        c = cats.setdefault(r["category"] + (" (grade and subject given)" if r.get("filters") else ""),
                            {"cases": 0, "passed": 0})
        c["cases"] += 1
        c["passed"] += r["passed"]
    cited = [s for r in results for s in r["sources"]]
    inv = {"total": len(sources), "by": {}}
    for s in sources:
        k = f"{s.get('subject')} | {s.get('grade_levels')} | {s.get('source_type')}"
        inv["by"][k] = inv["by"].get(k, 0) + 1
    return {
        "results": results, "categories": cats,
        "passed": sum(r["passed"] for r in results), "cases": len(results),
        "fabricated_citations": sum(r["fabricated"] for r in results),
        "substitutions": sum(1 for r in results if r["category"] in ("not_covered", "no_answer") and not r["passed"]),
        "sources_cited": len(cited),
        "sources_active": sum(1 for s in cited if s["active"]),
        "sources_with_checksum": sum(1 for s in cited if s["checksum"]),
        "official_sources_cited": sum(1 for s in cited if s["source_type"] in
                                      ("official_syllabus", "teacher_guide", "student_guide")),
        "mapping": {k: sum(1 for r in results if not r.get("filters") and r.get("mapping") == k)
                    for k in ("right", "other_grade", "wrong_subject")},
        "p95_seconds": sorted(lat)[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))] if lat else 0.0,
        "inventory": inv,
    }


LABEL = {"covered": "Covered (Grade 11-12 Biology, Chemistry, Physics)",
         "covered (grade and subject given)": "Covered, with grade and subject given",
         "not_covered": "Not covered yet (Grades 7-10, other subjects)",
         "no_answer": "Cannot be answered from the curriculum",
         "incorrect": "Incorrect premise",
         "incorrect (grade and subject given)": "Incorrect premise, with grade and subject given"}


def _cell(s: str, n: int = 400) -> str:
    s = re.sub(r"\s+", " ", s or "").strip().replace("|", "/")
    return s if len(s) <= n else s[:n] + " …"


def markdown(rep: dict[str, Any], when: str, who: str, pilot: Optional[list[dict[str, Any]]] = None) -> str:
    L = [f"# NEXA-M4-004 curriculum demo: {when}", "",
         f"Knowledge Service on the DGX, asked as {who}. Read-only: nothing was ingested or changed.", "",
         "## Summary", "",
         "| Category | Passed automatic check |", "|---|---|"]
    for c, v in rep["categories"].items():
        L.append(f"| {LABEL.get(c, c)} | {v['passed']}/{v['cases']} |")
    L += ["",
          f"- Fabricated citations: {rep['fabricated_citations']}",
          f"- Silent substitutions (an answer from the curriculum where none should exist): {rep['substitutions']}",
          f"- Cited sources: {rep['sources_cited']}; active versions {rep['sources_active']}; with a content "
          f"checksum {rep['sources_with_checksum']}; official syllabus or guide {rep['official_sources_cited']}",
          f"- Grounded answers without grade or subject: sources from the right course {rep['mapping']['right']}, "
          f"another grade of the same subject {rep['mapping']['other_grade']}, another subject "
          f"{rep['mapping']['wrong_subject']}",
          f"- p95 seconds per answer: {rep['p95_seconds']:.1f}", "",
          "## What NEXA holds", "", f"{rep['inventory']['total']} active sources.", "",
          "| Subject / grades / type | Sources |", "|---|---|"]
    for k, v in sorted(rep["inventory"]["by"].items(), key=lambda kv: -kv[1]):
        L.append(f"| {k} | {v} |")
    if pilot:
        ok = [p for p in pilot if "words" in p]
        L += ["", "## S3 pilot (31 files from `POM-NSOE Synchronization/`)", "",
              "| # | Course | Type | Words | Already in NEXA (by content) | S3 ETag | Last modified |", "|---|---|---|---|---|---|---|"]
        for p in ok:
            L.append(f"| {p['n']} | {p['course']} | {p['file'].rsplit('.', 1)[-1]} | {p['words']} | "
                     f"{p['content_hits']}/{p['probes']} | `{p['etag'][:12]}` | {p['last_modified'][:10]} |")
    L += ["", "## Sign-off sheet: academic correctness (CDD reviewers)", "",
          "Mark each answer: **C** correct, **P** partly correct, **I** incorrect, **R** correct refusal. "
          "For a refusal of a covered question, say whether the curriculum does cover it.", ""]
    for c in LABEL:
        rows = [r for r in rep["results"]
                if r["category"] + (" (grade and subject given)" if r.get("filters") else "") == c]
        if not rows:
            continue
        L += [f"### {LABEL[c]}", "",
              "| # | Question | NEXA's answer | Sources | Auto | Reviewer (C/P/I/R) | Comment |",
              "|---|---|---|---|---|---|---|"]
        for r in rows:
            src = "<br>".join(f"{s['marker']}: {_cell(s['title'] or '', 70)} ({s['source_type']}, "
                              f"{s['subject']} {s['grades']})" for s in r["sources"]) or "none"
            ans = _cell(r["answer"]) if r["status"] in GROUNDED else f"*{r['status']}* " + _cell(r["answer"], 160)
            auto = "ok" if r["passed"] else "MISS: " + _cell("; ".join(r["issues"]), 160)
            L.append(f"| {r['id']} | {_cell(r['question'], 200)} | {ans} | {src} | {auto} |  |  |")
        L.append("")
    L += ["## Sign-off: source integrity (digitization team)", "",
          "| Check | Yes / No | Comment |", "|---|---|---|",
          "| The six S3 courses are the current, complete versions of the Grade 11-12 STEM courses |  |  |",
          "| The sources NEXA cites above are the right documents for their course, grade and topic |  |  |",
          "| The official syllabi and teacher/student guides indexed in NEXA are the current editions |  |  |",
          "| Grades 7-10 and other subjects: not digitized yet (expected date, if known) |  |  |",
          "| The `.mbz` course backups contain no learner data, or are confirmed out of scope |  |  |",
          "| Bucket versioning can be enabled for the curriculum folder |  |  |", "",
          "| Role | Name | Signature | Date |", "|---|---|---|---|",
          "| CDD reviewer |  |  |  |", "| CDD reviewer |  |  |  |",
          "| Digitization team |  |  |  |", "| NEXA (Yasaru) |  |  |  |", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8103")
    ap.add_argument("--school", default="pomnsoe")
    ap.add_argument("--role", default="teacher", choices=["teacher", "student", "admin"])
    ap.add_argument("--out", default=os.path.expanduser("~/nexa-evidence/m4-004/demo"))
    ap.add_argument("--no-filtered", action="store_true", help="skip the second pass with grade and subject")
    ap.add_argument("--pilot", default=os.path.expanduser("~/nexa-evidence/m4-004/pilot/pilot-check.json"))
    a = ap.parse_args(argv)
    import httpx

    from v1.identity.tokens import issue_token
    h = {"Authorization": "Bearer " + issue_token(f"eval:curriculum-demo-{a.role}", [a.role], "user", 3600,
                                                  extra={"school_id": a.school})}
    c = httpx.Client(base_url=a.url.rstrip("/"), headers=h, timeout=180)
    r = c.get("/v1/knowledge/sources")
    if r.status_code != 200:
        print(f"cannot list sources: HTTP {r.status_code}")
        return 2
    sources = r.json()["sources"]

    def post(q: str, filters: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        body: dict[str, Any] = {"question": q, "general": False}
        if filters:
            body["filters"] = filters
        resp = c.post("/v1/knowledge/answer", json=body)
        resp.raise_for_status()
        return resp.json()
    print(f"Curriculum demo on {a.url} as {a.role} at {a.school}: {len(CASES)} questions, "
          f"covered and incorrect-premise ones also with grade and subject given")
    rep = run(post, sources, progress=print, filtered=not a.no_filtered)
    pilot = None
    if os.path.exists(a.pilot):
        with open(a.pilot) as fh:
            pilot = json.load(fh)
    when = time.strftime("%Y-%m-%d %H:%M")
    os.makedirs(a.out, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    md = os.path.join(a.out, f"nexa-curriculum-demo-{stamp}.md")
    js = os.path.join(a.out, f"nexa-curriculum-demo-{stamp}.json")
    with open(md, "w") as fh:
        fh.write(markdown(rep, when, f"a {a.role} at {a.school}", pilot))
    with open(js, "w") as fh:
        json.dump(rep, fh, indent=2, default=str)
    print()
    for k, v in rep["categories"].items():
        print(f"  {LABEL.get(k, k):<56} {v['passed']}/{v['cases']}")
    print(f"  fabricated citations {rep['fabricated_citations']}; silent substitutions {rep['substitutions']}; "
          f"cited sources {rep['sources_cited']} (active {rep['sources_active']}, checksum "
          f"{rep['sources_with_checksum']}, official {rep['official_sources_cited']}); p95 {rep['p95_seconds']:.1f} s")
    m = rep["mapping"]
    print(f"  without grade or subject, grounded answers cited: the right course {m['right']}, another grade "
          f"{m['other_grade']}, another subject {m['wrong_subject']}")
    print(f"\nreport and sign-off sheet: {md}\n                           {js}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
