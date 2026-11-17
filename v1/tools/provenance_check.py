"""Verify citation provenance end to end on a running Knowledge Service (M2 exit).

Read-only. Asks the approved, answerable M2-008 curriculum questions through
/v1/knowledge/answer and, for every citation an answer uses, checks that:

  1. the chunk exists (GET /v1/knowledge/chunks/{chunk_id}) and is active;
  2. the citation's quote is the chunk's own text (the opening of the passage);
  3. the citation's source version and checksum are the chunk's source version
     and checksum;
  4. the source version is approved (approved_by recorded) and active;
  5. the original file is stored, and its bytes hash (sha256) to that checksum
     (GET /v1/knowledge/sources/{version_id}/original).

Any failure is listed with the question, the marker and what did not match.

    (set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \\
     python v1/tools/provenance_check.py --base-url http://127.0.0.1:8103 --out ~/nexa-evidence)
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from v1.knowledge.benchmark import evalsuite as ev  # noqa: E402

CHECKS = ("chunk_found", "quote_matches", "version_matches", "approved_active", "original_hash")


def _norm(t: str) -> str:
    return " ".join(re.findall(r"\w+", (t or "").lower()))


def check_citation(c: dict[str, Any], get_chunk, get_original, cache: dict[str, Any]) -> dict[str, Any]:
    """Results of the five checks for one used citation. get_chunk(chunk_id)
    and get_original(version_id) return parsed JSON / bytes, or None."""
    p = c.get("provenance") or {}
    r: dict[str, Any] = {"marker": c.get("marker"), "chunk_id": p.get("chunk_id"), "title": p.get("title")}
    ch = get_chunk(p.get("chunk_id")) if p.get("chunk_id") else None
    r["chunk_found"] = bool(ch) and bool(ch.get("active", True))
    if not ch:
        return {**r, **{k: False for k in CHECKS[1:]}, "detail": "chunk not found"}
    q, text = _norm(c.get("quote") or ""), _norm(ch.get("text") or "")
    r["quote_matches"] = bool(q) and q[:200] in text
    src = ch.get("source") or {}
    r["version_matches"] = (p.get("version_id") == src.get("version_id")
                            and p.get("content_checksum") == src.get("content_checksum"))
    r["approved_active"] = bool(src.get("approved_by")) and src.get("status") == "active"
    vid = src.get("version_id")
    if vid not in cache:
        data = get_original(vid) if vid else None
        cache[vid] = ("sha256:" + hashlib.sha256(data).hexdigest()) if data is not None else None
    r["original_hash"] = cache[vid] is not None and cache[vid] == src.get("content_checksum")
    fails = [k for k in CHECKS if not r[k]]
    if fails:
        r["detail"] = "failed: " + ", ".join(fails)
    return r


def run(args) -> dict[str, Any]:
    import httpx

    from v1.identity import tokens
    ds = ev.load_dataset(args.dataset)
    items = [i for i in ds["items"] if i["suite"] == "corpus" and i.get("approved", True)
             and i["type"] in ("exact_fact", "conceptual", "ambiguous")][: args.limit or None]
    h = {"Authorization": "Bearer " + tokens.issue_token("eval:provenance-check", ["platform_admin"], "user", 3600)}
    base = args.base_url.rstrip("/")
    rows: list[dict[str, Any]] = []
    cache: dict[str, Any] = {}
    with httpx.Client(timeout=300) as c:
        def get_chunk(cid):
            r = c.get(f"{base}/v1/knowledge/chunks/{cid}", headers=h)
            return r.json() if r.status_code == 200 else None

        def get_original(vid):
            r = c.get(f"{base}/v1/knowledge/sources/{vid}/original", headers=h)
            return r.content if r.status_code == 200 else None
        answered = 0
        for n, it in enumerate(items, 1):
            body = {"question": it["question"]}
            if it.get("context_filters"):
                body["filters"] = it["context_filters"]
            a = c.post(f"{base}/v1/knowledge/answer", headers=h, json=body).json()
            used = [x for x in (a.get("citations") or []) if x.get("used")]
            answered += bool(used)
            for cit in used:
                rows.append({"question_id": it["id"], **check_citation(cit, get_chunk, get_original, cache)})
            if n % 10 == 0:
                print(f"  provenance: {n}/{len(items)}", flush=True)
    totals = {k: sum(1 for r in rows if r[k]) for k in CHECKS}
    return {"tool": "provenance_check", "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "base_url": base, "questions": len(items), "answered_with_citations": answered,
            "citations_checked": len(rows), "passed": totals, "versions_hashed": len(cache),
            "all_passed": bool(rows) and all(totals[k] == len(rows) for k in CHECKS),
            "failures": [r for r in rows if r.get("detail")], "citations": rows}


def markdown(rep: dict[str, Any]) -> str:
    n = rep["citations_checked"] or 1
    L = [f"# Citation provenance check ({rep['generated_at']}, {rep['base_url']})", "",
         f"**{'PASS' if rep['all_passed'] else 'FAIL'}**: {rep['citations_checked']} citations used in "
         f"{rep['answered_with_citations']} of {rep['questions']} answers; {rep['versions_hashed']} source "
         f"versions' original files hashed.", "", "| Check | Passed |", "|---|---|"]
    labels = {"chunk_found": "Cited chunk exists and is active",
              "quote_matches": "Quote is the chunk's own text",
              "version_matches": "Citation's source version and checksum match the chunk's",
              "approved_active": "Source version approved (approver recorded) and active",
              "original_hash": "Original file stored; its sha256 equals the recorded checksum"}
    L += [f"| {labels[k]} | {rep['passed'][k]}/{rep['citations_checked']} ({rep['passed'][k] / n:.0%}) |"
          for k in CHECKS]
    if rep["failures"]:
        L += ["", "| Question | Marker | Source | Problem |", "|---|---|---|---|"]
        L += [f"| {f['question_id']} | {f['marker']} | {f.get('title')} | {f['detail']} |" for f in rep["failures"]]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8103")
    ap.add_argument("--dataset", default=ev.DEFAULT_DATASET)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if not os.environ.get("NEXA_IDENTITY_SECRET"):
        print("load the identity settings first (NEXA_IDENTITY_SECRET)", file=sys.stderr)
        return 2
    rep = run(args)
    os.makedirs(args.out, exist_ok=True)
    stem = os.path.join(args.out, f"nexa-provenance-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}")
    with open(stem + ".json", "w") as fh:
        json.dump(rep, fh, indent=2)
    md = markdown(rep)
    with open(stem + ".md", "w") as fh:
        fh.write(md)
    print(md)
    print(f"report: {stem}.md / .json")
    return 0 if rep["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
