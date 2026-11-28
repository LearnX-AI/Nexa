"""Curriculum Intelligence Agent evaluation (NEXA-M4-004).

Offline (default): every case in v1/tools/resilience/curriculum_cases.py in
the isolated NEXA: mapping, grounding, boundary, safety and integration.
Exits 1 if any case fails. CI runs it.

    python -m v1.tools.curriculum_eval --out ~/nexa-evidence

Live (--live, on the DGX, once nexa-curriculum-intelligence is approved and
active): the workflow through the API gateway as a teacher at the school,
with the real model and curriculum:

- map: the twelve covered questions of the curriculum demo pack (two per
  Grade 11-12 Biology, Chemistry and Physics course), with grade and subject
  left as 'any', so the mapping itself must find them;
- not covered: Grades 7-10 and other subjects must be not_covered, never
  mapped to another grade or subject;
- alignment and progression: one per subject, grounded and cited;
- outcomes: every outcome code listed belongs to the mapped grade.

    (set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \\
     python -m v1.tools.curriculum_eval --live --out ~/nexa-evidence)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

CITE = re.compile(r"\[S\d+\]")
LIVE = {"mapping_min": 0.9, "not_covered_min": 1.0, "outcomes_grade_min": 1.0, "tasks_grounded_min": 0.9,
        "p95_s_max": 180, "violations_max": 0}
TASK_CASES = [("alignment", "Biology", "11", "My lesson: students read about Mendel's pea plant experiments and "
               "draw a Punnett square for one monohybrid cross."),
              ("progression", "Chemistry", "11", "Strong and weak acids and their degree of ionisation"),
              ("progression", "Physics", "12", "Transverse and longitudinal waves")]


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M")


def offline(out: str, only: str | None) -> int:
    from v1.tools.resilience_eval import _isolate
    _isolate()
    import contextlib
    import io
    from v1.tools.resilience import curriculum_cases as cc
    with contextlib.redirect_stdout(io.StringIO()):
        rep = cc.report(only)
    os.makedirs(out, exist_ok=True)
    md = os.path.join(out, f"nexa-curriculum-eval-{_stamp()}.md")
    js = os.path.join(out, f"nexa-curriculum-eval-{_stamp()}.json")
    with open(md, "w") as fh:
        fh.write(cc.markdown(rep))
    with open(js, "w") as fh:
        json.dump(rep, fh, indent=2, default=str)
    print(f"{rep['passed']}/{rep['cases']} Curriculum Intelligence cases passed in {rep['seconds']} s; "
          f"safety invariant violations: {len(rep['violations'])}")
    for c, v in rep["categories"].items():
        print(f"  {'✔' if v['passed'] == v['cases'] else '✘'} {c:<12} {v['passed']}/{v['cases']}")
    for r in rep["results"]:
        if not r["passed"]:
            print(f"  ✘ {r['id']}: {str(r['detail'])[:160]}")
    print(f"\nCurriculum Intelligence (offline): {'PASS' if rep['meets'] else 'FAIL'}\nreport: {md}\n        {js}")
    return 0 if rep["meets"] else 1


def p95(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))] if xs else 0.0


def parse_mapping(out: str) -> dict:
    def field(name):
        m = re.search(rf"^- {name}: (.+)$", out or "", re.M)
        return m.group(1).strip() if m else None
    codes = field("Official learning outcomes") or ""
    also = re.findall(r"Grade (\d+)", field("Also covered in") or "")
    return {"grade": field("Grade"), "subject": field("Subject"), "unit": field("Unit"), "topic": field("Topic"),
            "outcomes": [] if codes in ("", "none found") else [c.strip() for c in codes.split(",")],
            "also_grades": also,
            "confidence": (re.search(r"\((high|low) confidence\)", out or "") or [None, None])[1]}


def live(url: str, school: str, out_dir: str) -> int:
    import httpx

    from v1.identity.tokens import issue_token
    from v1.tools.curriculum_demo import CASES as DEMO

    def tok(sub, roles):
        return {"Authorization": "Bearer " + issue_token(sub, roles, "user", 7200, extra={"school_id": school})}
    teacher = tok("eval:curriculum-teacher", ["teacher"])
    student = tok("eval:curriculum-student", ["student"])
    c = httpx.Client(base_url=url.rstrip("/"), timeout=300)
    wf = "nexa-wf-curriculum-intelligence"
    info = c.get("/v1/registry/agents/nexa-curriculum-intelligence", headers=teacher).json()
    if info.get("state") != "active":
        print(f"nexa-curriculum-intelligence is not active on this host (current {info.get('version')} "
              f"{info.get('state')}): approve and activate it first, then run the live evaluation.")
        return 2
    lat, violations = [], []

    def run(task, text, grade="any", subject="any", who=None):
        t0 = time.time()
        r = c.post(f"/v1/workflows/{wf}/runs?wait=false",
                   json={"input": {"task": task, "text": text, "grade": grade, "subject": subject}},
                   headers=who or teacher)
        if r.status_code >= 300:
            return {"status": f"refused {r.status_code}", "output": "", "path": []}
        rid = r.json()["run_id"]
        res = {}
        for _ in range(200):
            res = c.get(f"/v1/workflow-runs/{rid}", headers=teacher).json()
            if res.get("status") in ("succeeded", "failed", "budget_exceeded", "rejected"):
                break
            time.sleep(2)
        lat.append(time.time() - t0)
        res["path"] = [t["step"] for t in res.get("trace", []) if t["kind"] == "step.started"]
        if res.get("status") in ("failed", "budget_exceeded") and res.get("output"):
            violations.append(f"{rid}: {res.get('status')} run released output")
        return res

    print(f"Live Curriculum Intelligence evaluation on {url} as a teacher at {school}\n\n1. Mapping "
          "(grade and subject left as 'any')")
    maps, grades_ok = [], []
    for cid, cat, subject, grade, question, _ in [d for d in DEMO if d[1] == "covered"]:
        r = run("map", question)
        m = parse_mapping(r.get("output") or "")
        # A topic taught in two grades may be mapped to either, provided the
        # other is named ("Also covered in"): the question gave no grade.
        ok = str(grade) in [m["grade"]] + m["also_grades"] and (m["subject"] or "").lower() == subject.lower()
        maps.append(ok)
        bad = [o for o in m["outcomes"] if not o.startswith(f"{m['grade']}.")]
        grades_ok.append(not bad)
        print(f"  {'ok  ' if ok else 'MISS'} {cid} expected {subject} G{grade}: got {m['subject']} G{m['grade']}"
              + (f" (also G{', G'.join(m['also_grades'])})" if m["also_grades"] else "")
              + f", outcomes {m['outcomes'] or '-'}, {m['confidence']} confidence, topic {m['topic']}"
              + (f"  OUTCOMES OF ANOTHER GRADE {bad}" if bad else ""))
    print("\n2. Not covered (Grades 7-10, other subjects): no substitution")
    nc = []
    for cid, cat, subject, grade, question, _ in [d for d in DEMO if d[1] == "not_covered"]:
        r = run("map", question, grade=str(grade), subject=subject)
        end = r["path"][-1] if r["path"] else None
        nc.append(end == "not_covered")
        print(f"  {'ok  ' if end == 'not_covered' else 'MISS'} {cid} {subject} G{grade}: {end}")
    print("\n3. Alignment and progression")
    tasks = []
    for task, subject, grade, text in TASK_CASES:
        r = run(task, text, grade=grade, subject=subject)
        out = r.get("output") or ""
        ok = r.get("status") == "succeeded" and r["path"][-1] in ("aligned", "progressed") and bool(CITE.search(out))
        tasks.append(ok)
        print(f"  {'ok  ' if ok else 'MISS'} {task:<12} {subject} G{grade}: {r.get('status')} at "
              f"{r['path'][-1] if r['path'] else '-'}, cited {bool(CITE.search(out))}")
    s = run("map", "Mendel's experiments with pea plants", grade="11", subject="Biology", who=student)
    boundary = s.get("status") == "refused 403"
    print(f"\n4. Students refused: {'ok' if boundary else 'MISS'}")
    if not boundary:
        violations.append("a student could run the Curriculum Intelligence workflow")

    def rate(xs):
        return sum(1 for x in xs if x) / len(xs) if xs else 1.0
    m = {"mapping": rate(maps), "not_covered": rate(nc), "outcomes_grade": rate(grades_ok),
         "tasks_grounded": rate(tasks), "p95_s": p95(lat), "violations": len(violations)}
    checks = [("mapped to the right grade and subject", m["mapping"], LIVE["mapping_min"], ">="),
              ("not covered, never substituted", m["not_covered"], LIVE["not_covered_min"], ">="),
              ("outcomes only of the mapped grade", m["outcomes_grade"], LIVE["outcomes_grade_min"], ">="),
              ("alignment and progression grounded", m["tasks_grounded"], LIVE["tasks_grounded_min"], ">="),
              ("p95 seconds per run", m["p95_s"], LIVE["p95_s_max"], "<="),
              ("safety violations", m["violations"], LIVE["violations_max"], "<=")]
    print()
    ok_all = True
    for name, v, lim, op in checks:
        ok = v >= lim if op == ">=" else v <= lim
        ok_all &= ok
        shown = f"{v:.0%}" if op == ">=" else (f"{v:.1f}" if isinstance(v, float) else str(v))
        print(f"  {'✔' if ok else '✘'} {name:<42} {shown:>6}   (threshold {op} {lim})")
    for v in violations:
        print(f"    violation: {v}")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"nexa-curriculum-eval-live-{_stamp()}.json")
    with open(path, "w") as fh:
        json.dump({"metrics": m, "thresholds": LIVE, "violations": violations, "school": school}, fh, indent=2)
    print(f"\nCurriculum Intelligence (live): {'MET' if ok_all else 'NOT MET'}\nreport: {path}")
    return 0 if ok_all else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.expanduser("~/nexa-evidence"))
    ap.add_argument("--only", help="offline: a category or case id prefix")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--url", default="http://127.0.0.1:8099")
    ap.add_argument("--school", default="pomnsoe")
    a = ap.parse_args(argv)
    return live(a.url, a.school, a.out) if a.live else offline(a.out, a.only)


if __name__ == "__main__":
    sys.exit(main())
