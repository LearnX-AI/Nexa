"""Run the resilience scenarios and score them against the thresholds (NEXA-M3-009)."""
from __future__ import annotations

import json
import os
import time
import traceback
from typing import Any, Optional

from . import sandbox
from .scenarios import CATEGORIES, SCENARIOS

THRESHOLDS = os.path.join(os.path.dirname(__file__), "thresholds.json")


def thresholds() -> dict[str, Any]:
    with open(THRESHOLDS) as fh:
        return json.load(fh)


def run(only: Optional[str] = None) -> dict[str, Any]:
    t0 = time.time()
    results = []
    for sc in SCENARIOS:
        if only and not (sc.id.startswith(only) or sc.category == only):
            continue
        s0 = time.time()
        sandbox.LAST_VIOLATIONS.clear()
        try:
            out = sc.fn()
            violations = list(sandbox.LAST_VIOLATIONS)        # checked inside the scenario's World
            ok, detail, runs = out.ok, out.detail, out.runs
        except Exception as exc:  # noqa: BLE001 - a crash is a failed scenario
            ok, detail, runs, violations = False, f"crashed: {type(exc).__name__}: {exc}", [], []
            detail += " | " + traceback.format_exc(limit=3).replace("\n", " ")[-300:]
        results.append({"id": sc.id, "category": sc.category, "title": sc.title, "critical": sc.critical,
                        "passed": ok and not violations, "behaviour_ok": ok, "detail": detail,
                        "violations": violations, "runs": runs, "seconds": round(time.time() - s0, 2)})
    return score(results, time.time() - t0)


def score(results: list[dict[str, Any]], seconds: float) -> dict[str, Any]:
    th = thresholds()["offline"]
    cats = {}
    for c in CATEGORIES:
        rs = [r for r in results if r["category"] == c]
        if not rs:
            continue
        rate = sum(r["passed"] for r in rs) / len(rs)
        cats[c] = {"scenarios": len(rs), "passed": sum(r["passed"] for r in rs), "rate": round(rate, 3),
                   "threshold": th["categories"].get(c, 1.0), "meets": rate >= th["categories"].get(c, 1.0)}
    violations = [v for r in results for v in r["violations"]]
    critical = [r for r in results if r["critical"] and r["category"] != "routing"]
    crit_rate = sum(r["passed"] for r in critical) / len(critical) if critical else 1.0
    meets = all(c["meets"] for c in cats.values()) and len(violations) <= th["safety_violations_max"] and \
        crit_rate >= th["critical_pass_rate_min"]
    return {"suite": "nexa-m3-009-resilience", "seconds": round(seconds, 1), "scenarios": len(results),
            "passed": sum(r["passed"] for r in results), "categories": cats,
            "critical_pass_rate": round(crit_rate, 3), "safety_violations": violations,
            "thresholds": thresholds(), "meets_thresholds": meets, "results": results}


def markdown(rep: dict[str, Any]) -> str:
    th = rep["thresholds"]
    lines = ["# NEXA multi-agent resilience evaluation (M3-009)", "",
             f"{rep['passed']}/{rep['scenarios']} scenarios passed in {rep['seconds']} s. "
             f"Thresholds ({th['status']}, version {th['version']}): "
             f"**{'MET' if rep['meets_thresholds'] else 'NOT MET'}**.", "",
             f"- Safety invariant violations: {len(rep['safety_violations'])} (allowed: "
             f"{th['offline']['safety_violations_max']})",
             f"- Critical scenarios (all but routing): {rep['critical_pass_rate']:.0%} (required: "
             f"{th['offline']['critical_pass_rate_min']:.0%})", "",
             "| Category | Passed | Rate | Threshold | |", "|---|---|---|---|---|"]
    for c, v in rep["categories"].items():
        lines.append(f"| {c} | {v['passed']}/{v['scenarios']} | {v['rate']:.0%} | {v['threshold']:.0%} | "
                     f"{'✔' if v['meets'] else '✘'} |")
    lines += ["", "## Scenarios", ""]
    for r in rep["results"]:
        mark = "✔" if r["passed"] else "✘"
        lines.append(f"- {mark} `{r['id']}` {r['title']}: {r['detail']}")
        for v in r["violations"]:
            lines.append(f"  - **invariant violated:** {v}")
    if rep["safety_violations"]:
        lines += ["", "## Safety invariant violations", ""] + [f"- {v}" for v in rep["safety_violations"]]
    return "\n".join(lines) + "\n"
