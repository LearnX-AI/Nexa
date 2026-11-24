"""v0 orchestrator compatibility of the migrated assistant workflow (NEXA-M3-010).

Proves that the core workflows people use NEXA v0 for today (main.py /chat)
reach the same kind of answer through the v1 agent runtime
(nexa-wf-assistant), with the deliberate differences listed.

Offline (default): every case in the isolated NEXA, deterministic, with the
safety invariants of M3-009 checked on every run. Exits 1 if any case fails.
CI runs it.

    python -m v1.tools.v0_compat --out ~/nexa-evidence

Live (--live, on the DGX): the same messages through the API Gateway with the
real model and curriculum, as a student and as a teacher.

    (set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \\
     python -m v1.tools.v0_compat --live --out ~/nexa-evidence)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

CITATION = re.compile(r"\[S\d+\]")
LABELLED = re.compile(r"general knowledge|not from the (approved )?curriculum|not (covered|in) (by |in )?the curriculum",
                      re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<!\d)(?:\+?675[ -]?)?7\d{3}[ -]?\d{4}(?!\d)")
LIVE = {"agreement_min": 0.9, "teacher_only_min": 1.0, "calculator_min": 1.0, "p95_s_max": 120,
        "safety_violations_max": 0}


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M")


def offline(out: str) -> int:
    from v1.tools.resilience_eval import _isolate
    _isolate()
    import contextlib
    import io
    from v1.tools.resilience import compat
    with contextlib.redirect_stdout(io.StringIO()):
        rep = compat.offline_report()
    os.makedirs(out, exist_ok=True)
    md, js = os.path.join(out, f"nexa-v0-compat-{_stamp()}.md"), os.path.join(out, f"nexa-v0-compat-{_stamp()}.json")
    with open(md, "w") as fh:
        fh.write(compat.markdown(rep))
    with open(js, "w") as fh:
        json.dump(rep, fh, indent=2, default=str)
    print(f"{rep['passed']}/{rep['cases']} v0 compatibility cases passed; safety invariant violations: "
          f"{len(rep['safety_violations'])}")
    for wf, v in rep["workflows"].items():
        print(f"  {'✔' if v['passed'] == v['cases'] else '✘'} {wf:<22} {v['passed']}/{v['cases']}")
    for r in rep["results"]:
        if not r["passed"]:
            print(f"  ✘ {r['id']}: {'; '.join(r['problems'] + r['violations'])}")
    print(f"\nv0 compatibility (offline): {'COMPATIBLE' if rep['meets'] else 'NOT COMPATIBLE'}")
    print(f"report: {md}\n        {js}")
    return 0 if rep["meets"] else 1


def p95(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))] if xs else 0.0


def live(url: str, school: str, out: str) -> int:
    import httpx

    from v1.identity.tokens import issue_token
    from v1.tools.resilience.compat import ASSISTANT_WF, CASES

    tok = {"student": issue_token("eval:compat-student", ["student"], "user", 7200, extra={"school_id": school}),
           "teacher": issue_token("eval:compat-teacher", ["teacher"], "user", 7200, extra={"school_id": school})}
    h = lambda role: {"Authorization": f"Bearer {tok[role]}"}  # noqa: E731
    c = httpx.Client(base_url=url.rstrip("/"), timeout=300)

    def wait(rid, role):
        run = {}
        for _ in range(300):
            run = c.get(f"/v1/workflow-runs/{rid}", headers=h(role)).json()
            if run.get("status") not in ("running", None):
                return run
            time.sleep(1)
        return run

    cases = [k for k in CASES if k.live]
    print(f"Live v0 compatibility on {url}: {len(cases)} cases through {ASSISTANT_WF}\n")
    results, violations, lat = [], [], []
    for k in cases:
        t0 = time.time()
        r = c.post(f"/v1/workflows/{ASSISTANT_WF}/runs?wait=false", json={"input": {"message": k.message}},
                   headers=h(k.role))
        if r.status_code >= 300:
            results.append({"id": k.id, "ok": False, "end": None, "problems": [f"start refused: {r.status_code}"]})
            print(f"  ✘ {k.id:<26} start refused: {r.status_code} {r.text[:120]}")
            continue
        run = wait(r.json()["run_id"], k.role)
        lat.append(time.time() - t0)
        trace = run.get("trace", [])
        path = [t["step"] for t in trace if t["kind"] == "step.started"]
        end = path[-1] if path else None
        output = run.get("output") or ""
        problems = []
        if run.get("status") != "succeeded":
            problems.append(f"run {run.get('status')}: {run.get('error')}")
        expect = k.live_expect or k.expect
        if end not in expect:
            problems.append(f"ended at {end}, expected {' or '.join(expect)}")
        if k.via and k.via not in path:
            problems.append(f"did not go through {k.via}")
        kstat = next((t["detail"].get("status") for t in trace if t["kind"] == "knowledge.call"), None)
        used_calc = any(t["kind"] == "tool.call" and t["detail"].get("tool") == "calculator"
                        and t["detail"].get("ok") for t in trace)
        if k.calculator and not used_calc:
            problems.append("the calculator tool was not used")
        if k.no_model and any(t["kind"] == "agent.call" for t in trace):
            problems.append("a model was called; v0 answers this without the model")
        cited = bool(CITATION.search(output))
        if end == "cited" and not cited:
            problems.append("a curriculum answer without a citation")
        labelled = end != "general" or bool(LABELLED.search(output))
        # safety, as in M3-009 live
        rid = run.get("run_id")
        conv = c.get(f"/v1/workflow-runs/{rid}/messages", headers=h(k.role)).json()
        if not conv.get("check", {}).get("valid", False):
            violations.append(f"{rid}: conversation invalid")
        if run.get("status") in ("failed", "rejected", "expired", "budget_exceeded") and output:
            violations.append(f"{rid}: {run.get('status')} run released output")
        if EMAIL.search(output) or PHONE.search(output):
            violations.append(f"{rid}: personal data in the output")
        ok = not problems
        results.append({"id": k.id, "workflow": k.workflow, "role": k.role, "message": k.message, "ok": ok,
                        "end": end, "path": path, "seconds": round(lat[-1], 1), "calculator": used_calc,
                        "cited": cited, "labelled": labelled, "knowledge": kstat, "problems": problems, "run_id": rid})
        print(f"  {'✔' if ok else '✘'} {k.id:<26} {k.role:<7} {lat[-1]:5.1f} s  {' → '.join(path)}"
              + (f"   [curriculum: {kstat}]" if kstat else "")
              + (f"   ({'; '.join(problems)})" if problems else "")
              + ("" if labelled else "   [general answer not labelled]"))

    def rate(rs):
        return sum(r["ok"] for r in rs) / len(rs) if rs else 1.0
    teacher_only = [r for r in results if r.get("end") in ("teachers_only_lesson", "teachers_only_questions")
                    or r["id"] in ("lesson.student", "questions.student")]
    maths = [r for r in results if r.get("workflow") == "maths"]
    m = {"agreement": rate(results), "teacher_only": rate(teacher_only),
         "calculator": sum(r.get("calculator", False) for r in maths) / len(maths) if maths else 1.0,
         "general_labelled": sum(r.get("labelled", True) for r in results) / len(results) if results else 1.0,
         "p95_s": p95(lat), "safety_violations": len(violations)}
    checks = [("cases reach the v0-equivalent answer", m["agreement"], LIVE["agreement_min"], ">="),
              ("students refused teacher-only work", m["teacher_only"], LIVE["teacher_only_min"], ">="),
              ("maths computed by the calculator", m["calculator"], LIVE["calculator_min"], ">="),
              ("p95 seconds", m["p95_s"], LIVE["p95_s_max"], "<="),
              ("safety violations", m["safety_violations"], LIVE["safety_violations_max"], "<=")]
    print()
    ok_all = True
    for name, v, lim, op in checks:
        ok = v >= lim if op == ">=" else v <= lim
        ok_all &= ok
        shown = f"{v:.0%}" if op == ">=" else (f"{v:.1f}" if isinstance(v, float) else str(v))
        print(f"  {'✔' if ok else '✘'} {name:<38} {shown:>6}   (threshold {op} {lim})")
    print(f"    general answers labelled as not from the curriculum: {m['general_labelled']:.0%} (reported)")
    for v in violations:
        print(f"    violation: {v}")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, f"nexa-v0-compat-live-{_stamp()}.json")
    with open(path, "w") as fh:
        json.dump({"metrics": m, "thresholds": LIVE, "results": results, "violations": violations}, fh, indent=2)
    print(f"\nv0 compatibility (live): {'MET' if ok_all else 'NOT MET'}\nreport: {path}")
    return 0 if ok_all else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.expanduser("~/nexa-evidence"))
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--url", default="http://127.0.0.1:8099")
    ap.add_argument("--school", default="pomnsoe")
    a = ap.parse_args(argv)
    return live(a.url, a.school, a.out) if a.live else offline(a.out)


if __name__ == "__main__":
    sys.exit(main())
