"""Draft curriculum outcome registry for CDD validation (NEXA-M4-004).

Read only. Lists every learning outcome code in the official syllabi the
Knowledge Service holds, with its statement, unit, source and page, grouped
by subject and grade, and writes a review sheet for the CDD curriculum
officers. The registry is a DRAFT until they validate it; NEXA uses outcome
codes only from the official syllabus (authority rule R3) whatever its state.

    (set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \\
     python -m v1.tools.outcome_registry --out ~/nexa-evidence/m4-004)
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
from typing import Any

CODE = re.compile(r"(?<![\d.])((?:1[0-2]|[7-9])\.\d{1,2}\.\d{1,2}(?:\.\d{1,2})?[a-z]?)(?![\d])")


def statement(text: str, code: str) -> str:
    """The outcome's own words: from the code to the end of its line or sentence."""
    i = (text or "").find(code)
    if i < 0:
        return ""
    rest = text[i + len(code):].lstrip(" :.-–\t")
    end = min([p for p in (rest.find("\n"), rest.find(". ")) if p >= 0] or [len(rest)])
    return re.sub(r"\s+", " ", rest[:end]).strip()[:300]


def build(sources: list[dict[str, Any]], chunks_of) -> dict[str, Any]:
    entries: dict[tuple[str, int, str], dict[str, Any]] = {}
    issues: list[str] = []
    for s in sources:
        if s.get("source_type") != "official_syllabus":
            continue
        grades = s.get("grade_levels") or []
        for ch in chunks_of(s["version_id"]):
            text = ch.get("text") or ""
            cur = ch.get("curriculum") or {}
            codes = list(dict.fromkeys(list(cur.get("outcome_codes") or []) + CODE.findall(text)))
            loc = ch.get("locator") or {}
            for code in codes:
                g = int(code.split(".")[0])
                if grades and g not in grades:
                    issues.append(f"{code} appears in {s.get('title')} (Grade {grades}): another grade's code, "
                                  "kept out of this grade")
                    continue
                key = (s.get("subject") or "?", g, code)
                e = entries.setdefault(key, {
                    "code": code, "subject": s.get("subject"), "grade": g, "statement": "",
                    "unit": " ".join(x for x in (cur.get("module_code"), cur.get("module_title")) if x) or None,
                    "topic": cur.get("topic_title"), "sources": []})
                st = statement(text, code)
                if len(st) > len(e["statement"]):
                    e["statement"] = st
                ref = f"{s.get('title')}, p. {loc.get('page_label') or loc.get('page_start') or '?'}"
                if ref not in e["sources"]:
                    e["sources"].append(ref)
    def order(code: str) -> list:
        # "11.1.2a" -> [(11, ""), (1, ""), (2, "a")]: numbers compare as numbers, letters after them
        return [(int(m.group(1)), m.group(2)) for m in re.finditer(r"(\d+)([a-z]?)", code)]
    out = sorted(entries.values(), key=lambda e: (str(e["subject"]), e["grade"], order(e["code"])))
    for e in out:
        if not e["statement"]:
            issues.append(f"{e['code']} ({e['subject']} Grade {e['grade']}): no statement found next to the code")
    counts = collections.Counter(f"{e['subject']} Grade {e['grade']}" for e in out)
    return {"status": "draft: not validated by CDD", "outcomes": out, "counts": dict(counts), "issues": issues}


def markdown(reg: dict[str, Any], when: str) -> str:
    L = [f"# NEXA curriculum outcome registry: DRAFT for CDD validation ({when})", "",
         "Every learning outcome code found in the official syllabi NEXA holds, with the statement next to it. "
         "NEXA maps content to these codes only (authority rule R3). Please mark each row: **Y** correct, "
         "**N** wrong or not an outcome, **E** statement needs editing (give the correct wording).", "",
         "| Subject and grade | Outcomes |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in sorted(reg["counts"].items())]
    current = None
    for e in reg["outcomes"]:
        head = f"{e['subject']} Grade {e['grade']}"
        if head != current:
            current = head
            L += ["", f"## {head}", "", "| Code | Statement | Unit | Source | CDD (Y/N/E) | Comment |",
                  "|---|---|---|---|---|---|"]
        st = (e["statement"] or "*(no statement found)*").replace("|", "/")
        L.append(f"| {e['code']} | {st} | {e['unit'] or ''} | {'; '.join(e['sources'][:2])} |  |  |")
    if reg["issues"]:
        L += ["", "## Points to check", ""] + [f"- {i}" for i in reg["issues"][:100]]
    L += ["", "| Role | Name | Signature | Date |", "|---|---|---|---|", "| CDD curriculum officer |  |  |  |",
          "| CDD curriculum officer |  |  |  |", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8103")
    ap.add_argument("--out", default=os.path.expanduser("~/nexa-evidence"))
    a = ap.parse_args(argv)
    import httpx

    from v1.identity.tokens import issue_token
    h = {"Authorization": "Bearer " + issue_token("ops:outcome-registry", ["admin"], "user", 1800)}
    c = httpx.Client(base_url=a.url.rstrip("/"), headers=h, timeout=120)
    r = c.get("/v1/knowledge/sources")
    if r.status_code != 200:
        print(f"cannot list sources: HTTP {r.status_code}")
        return 2

    def chunks_of(vid):
        g = c.get(f"/v1/knowledge/sources/{vid}/chunks")
        return g.json().get("chunks", []) if g.status_code == 200 else []
    reg = build(r.json()["sources"], chunks_of)
    os.makedirs(a.out, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    md = os.path.join(a.out, f"nexa-outcome-registry-draft-{stamp}.md")
    js = os.path.join(a.out, f"nexa-outcome-registry-draft-{stamp}.json")
    with open(md, "w") as fh:
        fh.write(markdown(reg, time.strftime("%Y-%m-%d")))
    with open(js, "w") as fh:
        json.dump(reg, fh, indent=2)
    print(f"{len(reg['outcomes'])} outcome codes in the official syllabi:")
    for k, v in sorted(reg["counts"].items()):
        print(f"  {v:>4}  {k}")
    print(f"points to check: {len(reg['issues'])}")
    print(f"\nreview sheet for CDD: {md}\n                      {js}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
