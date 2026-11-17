"""Why does a live NEXA service miss? (NEXA-M2-008 follow-up)

Read-only. For every approved, answerable M2-008 corpus question, asks the
running Knowledge Service for its top 50 passages and reports, for each
question whose answering passage is not in the top 5:

- where the answering passage is (rank 6-50, or not found at all);
- what took the top 5 places: title, source type, grade, LMS course,
  authority tier, similarity, and whether it is a near copy of a passage
  ranked above it.

It ends with totals: how much of the top 5 is LMS content, content of
another grade than the question's, or a near copy. These say which fix
helps: duplicates, grade, authority, or the passage being absent.

    (set -a; . <(sudo cat /etc/nexa/identity.env /etc/nexa/knowledge.env); set +a; \\
     python v1/tools/nexa_diagnose.py --base-url http://127.0.0.1:8103 --out ~/nexa-evidence)
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import sys
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from v1.knowledge.benchmark import evalsuite as ev  # noqa: E402

DEEP = 50


def _shingles(text: str, n: int = 5) -> set[str]:
    import re
    w = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {" ".join(w[i:i + n]) for i in range(max(1, len(w) - n + 1))}


def containment(a: set[str], b: set[str]) -> float:
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def grades_of(md: dict[str, Any]) -> set[int]:
    raw = md.get("grade_levels")
    if raw is None:
        return set()
    parts = raw if isinstance(raw, list) else str(raw).split(",")
    out = set()
    for p in parts:
        try:
            out.add(int(str(p).strip()))
        except ValueError:
            pass
    return out


def describe(r: dict[str, Any], rank: int, above: list[set[str]]) -> dict[str, Any]:
    md = r.get("metadata") or {}
    sh = _shingles(r.get("text") or "")
    dup = max((containment(sh, o) for o in above), default=0.0)
    return {"rank": rank, "file": ev.file_of_result(r), "source_type": md.get("source_type"),
            "grades": sorted(grades_of(md)), "lms_course": md.get("lms_course_id"),
            "tier": md.get("authority_tier"), "similarity": round(float(r.get("score") or 0), 3),
            "near_copy_of_above": round(dup, 2), "text": " ".join((r.get("text") or "").split())[:140]}


def run(args) -> dict[str, Any]:
    import httpx

    from v1.identity import tokens
    ds = ev.load_dataset(args.dataset)
    items = [i for i in ds["items"] if i["suite"] == "corpus" and i.get("approved", True)
             and i["type"] in ("exact_fact", "conceptual", "ambiguous", "spelling")]
    extra = {"school_id": args.school_id} if args.school_id else None
    tok = tokens.issue_token("eval:nexa-diagnose", [args.as_role], "user", 3600, extra=extra)
    h = {"Authorization": f"Bearer {tok}"}
    base = args.base_url.rstrip("/")
    rows, totals = [], collections.Counter()
    with httpx.Client(timeout=180) as c:
        health = c.get(base + "/health").json()
        for it in items:
            body = {"query": it["question"], "top_k": DEEP, "explain": True}
            if it.get("context_filters"):
                body["filters"] = it["context_filters"]
            if args.course_context:
                ctx = {k: v for k, v in (("grades", [int(it["grade"])] if it.get("grade") else None),
                                         ("subjects", [it["subject"]] if it.get("subject") else None)) if v}
                if ctx:
                    body["context"] = ctx
            q = c.post(base + "/v1/knowledge/query", headers=h, json=body)
            q.raise_for_status()
            q = q.json()
            res = q["results"]
            rank = ev.hit_rank(res, it)
            want = {int(it["grade"])} if it.get("grade") else set()
            above: list[set[str]] = []
            top = []
            for i, r in enumerate(res[:5], 1):
                d = describe(r, i, above)
                above.append(_shingles(r.get("text") or ""))
                top.append(d)
                totals["top5"] += 1
                totals["top5_lms"] += d["source_type"] == "lms_content"
                totals["top5_other_grade"] += bool(want and d["grades"] and not (set(d["grades"]) & want))
                totals["top5_near_copy"] += d["near_copy_of_above"] >= 0.6
                totals["top5_expected_file"] += d["file"] in it["files"]
            totals["questions"] += 1
            if rank is None or rank > 5:
                totals["misses"] += 1
                totals["miss_absent" if rank is None else "miss_deep"] += 1
                rows.append({"id": it["id"], "type": it["type"], "subject": it.get("subject"),
                             "grade": it.get("grade"), "question": it["question"], "expected": it["files"],
                             "answer_rank": rank, "diversity": (q.get("retrieval") or {}).get("diversity"),
                             "top5": top})
    return {"tool": "nexa_diagnose", "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "base_url": base, "service": {"version": health.get("version"), "index": health.get("active_index")},
            "dataset_version": ds["version"], "totals": dict(totals), "misses": rows}


def markdown(rep: dict[str, Any]) -> str:
    t = rep["totals"]
    n5 = t.get("top5", 0) or 1
    L = [f"# Why the live service misses ({rep['generated_at']}, {rep['base_url']}, index {rep['service']['index']})",
         "", f"Questions: {t.get('questions', 0)}; answering passage not in the top 5: {t.get('misses', 0)} "
         f"(at rank 6-{DEEP}: {t.get('miss_deep', 0)}; not in the top {DEEP}: {t.get('miss_absent', 0)}).", "",
         "| Top-5 places held by | Share |", "|---|---|",
         f"| the expected source | {t.get('top5_expected_file', 0) / n5:.0%} |",
         f"| LMS content | {t.get('top5_lms', 0) / n5:.0%} |",
         f"| content of another grade than the question's | {t.get('top5_other_grade', 0) / n5:.0%} |",
         f"| a near copy (60%+) of a passage ranked above it | {t.get('top5_near_copy', 0) / n5:.0%} |", ""]
    for m in rep["misses"]:
        L += [f"## {m['id']} ({m['type']}, {m['subject']} {m['grade']}): answer at rank {m['answer_rank']}",
              "", f"{m['question']}  ", f"Expected: {', '.join(m['expected'])}. Diversity: {m['diversity']}", "",
              "| # | File | Type | Grades | LMS course | Tier | Sim | Copy | Text |", "|---|---|---|---|---|---|---|---|---|"]
        for d in m["top5"]:
            L.append(f"| {d['rank']} | {d['file']} | {d['source_type']} | {d['grades']} | {d['lms_course']} | "
                     f"{d['tier']} | {d['similarity']} | {d['near_copy_of_above']} | {d['text'][:90]} |")
        L.append("")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8103")
    ap.add_argument("--dataset", default=ev.DEFAULT_DATASET)
    ap.add_argument("--out", required=True)
    ap.add_argument("--as-role", default="platform_admin")
    ap.add_argument("--school-id", default="")
    ap.add_argument("--course-context", action="store_true",
                    help="send each question's subject and grade as course context")
    args = ap.parse_args()
    if not os.environ.get("NEXA_IDENTITY_SECRET"):
        print("load the service settings first (NEXA_IDENTITY_SECRET)", file=sys.stderr)
        return 2
    rep = run(args)
    os.makedirs(args.out, exist_ok=True)
    stem = os.path.join(args.out, f"nexa-diagnose-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}")
    with open(stem + ".json", "w") as fh:
        json.dump(rep, fh, indent=2)
    md = markdown(rep)
    with open(stem + ".md", "w") as fh:
        fh.write(md)
    print(md)
    print(f"report: {stem}.md / .json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
