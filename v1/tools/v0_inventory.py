"""NEXA-M2-010: inventory of NEXA v0 knowledge assets, and a decision for each.

Read-only. Looks at a v0 installation (its folder and its Chroma store) and,
optionally, at a v1 Knowledge Service, and writes a report saying, for every
v0 file and every document inside the v0 vector store, what happens to it in
v1 and why:

  retained     the same file (identical bytes, sha256) is already a governed v1
               source; v1 reprocessed it from the file, the v0 index is not used
  reprocess    a curriculum file v1 does not hold yet: ingest it from the file,
               with v1 metadata, through approval (never from the v0 index)
  resync       LMS material that exists in v0 only as chunks inside Chroma:
               bring it in again from the LMS (its authoritative source)
  review       a file whose subject, type or origin cannot be read from its name
  deprecated   duplicates, superseded copies, and the v0 vector index itself
  excluded     learner data (chats, memory, uploads) and generated output:
               never knowledge, never migrated

Why the v0 index is never copied: its vectors were made with v0 chunking
(1,500 characters, 300 overlap, page-split PDFs) and carry no source version,
checksum, approval, scope or access classification, so nothing in it can be
cited or governed the way v1 requires.

    python v1/tools/v0_inventory.py --v0-dir ~/Nexa --out ~/nexa-evidence
    (set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \\
     python v1/tools/v0_inventory.py --v0-dir ~/Nexa --v1-url http://127.0.0.1:8103 --out ~/nexa-evidence)
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

KNOWLEDGE_EXT = (".pdf", ".docx", ".pptx")
LEARNER_NAMES = {"user_memory.json"}
LEARNER_DIRS = {"uploads"}              # files learners attached in chat
GENERATED_DIRS = {"assets"}             # answer PDFs and images v0 generated

# v0's own file-name rules (main.py parse_curriculum_filename), and the v1 type each maps to.
NAME_RE = re.compile(r"^STEM[\s_-]*(?P<subject>[A-Za-z]+)[\s_-]*Term[\s_-]*(?P<term>\d+)"
                     r"[\s_-]*(?P<doctype>[A-Za-z]+)[\s_-]*Gr[\s_-]*(?P<grade>\d{1,2})$", re.I)
SUBJECTS = {"bio": "Biology", "biology": "Biology", "chem": "Chemistry", "chemistry": "Chemistry",
            "physics": "Physics", "phy": "Physics", "eng": "Engineering", "engineering": "Engineering",
            "tech": "Technology", "technology": "Technology", "maths": "Mathematics", "math": "Mathematics"}
DOCTYPES = {"studentguide": "Student Guide", "teacherguide": "Teacher Guide", "syllabus": "Syllabus",
            "syllubus": "Syllabus", "srb": "Student Guide", "trb": "Teacher Guide", "tg": "Teacher Guide"}
LESSON_RE = re.compile(r"^s\d+_cm(?P<cm>\d+)_(?P<code>\d+\.\d+)_", re.I)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def parse_name(name: str) -> dict[str, Any]:
    """v0's reading of a file name, plus the v1 metadata it maps to."""
    stem = os.path.splitext(name)[0].strip()
    info: dict[str, Any] = {"subject": None, "grade": None, "term": None, "v0_doctype": None}
    m = NAME_RE.match(stem)
    if m:
        info.update(subject=SUBJECTS.get(m["subject"].lower(), m["subject"].title()), grade=int(m["grade"]),
                    term=int(m["term"]), v0_doctype=DOCTYPES.get(m["doctype"].lower(), m["doctype"].title()))
    else:
        flat = re.sub(r"[-_ ]", "", stem.lower())
        g = re.search(r"gr(?:ade)?(9|1[0-3])", flat)
        info["grade"] = int(g.group(1)) if g else None
        info["subject"] = next((v for k, v in SUBJECTS.items() if k in flat), None)
        info["v0_doctype"] = next((v for k, v in DOCTYPES.items() if k in flat), None)
        ls = LESSON_RE.match(name)
        if ls:   # EduNeX lesson file: s04_cm8879_12.2_...
            info.update(v0_doctype="LMS Material", lms_course_module=f"cm{ls['cm']}",
                        grade=int(ls["code"].split(".")[0]), subject=info["subject"] or "Biology")
    info["v1"] = v1_mapping(name)
    return info


def v1_mapping(name: str) -> dict[str, Any] | None:
    """The metadata v1 ingestion infers from this file name (v1.knowledge.curriculum),
    so the inventory shows exactly what ingestion would record."""
    from v1.knowledge.curriculum import draft_from_filename
    from v1.knowledge.metadata import SOURCE_TYPE_TIER, SourceType
    d = draft_from_filename(name)
    st = d.get("source_type")
    if not st:
        return None
    st = SourceType(st)
    val = lambda x: getattr(x, "value", x)  # noqa: E731
    return {"source_id": d.get("source_id"), "source_type": st.value,
            "authority_tier": int(SOURCE_TYPE_TIER[st].value),
            "access_classification": val(d.get("access_classification")),
            "subject": val(d.get("subject")), "grade_levels": d.get("grade_levels") or [], "term": d.get("term"),
            "title": d.get("title"), "lms_item": (d.get("lms") or {}).get("item_id")}


def scan_files(v0_dir: str) -> list[dict[str, Any]]:
    """What v0 itself reads: the curriculum files at the top of its folder
    (main.py: glob <workspace>/*.pdf), plus its uploads/ (learner attachments)
    and assets/ (generated output) folders and user_memory.json. Nothing
    deeper: on the DGX the v0 folder is the repository, whose v1/ and docs/
    are not v0 knowledge."""
    out = []
    try:
        top = sorted(os.listdir(v0_dir))
    except OSError:
        return out
    entries = [(f, os.path.join(v0_dir, f), "") for f in top if os.path.isfile(os.path.join(v0_dir, f))]
    for sub in sorted(LEARNER_DIRS | GENERATED_DIRS):
        d = os.path.join(v0_dir, sub)
        if os.path.isdir(d):
            for dirpath, dirnames, filenames in os.walk(d):
                dirnames[:] = [x for x in dirnames if not x.startswith(".")]
                entries += [(f, os.path.join(dirpath, f), sub) for f in sorted(filenames)]
    for f, path, where in entries:
        low = f.lower()
        kind = ("learner" if f in LEARNER_NAMES or where in LEARNER_DIRS else
                "generated" if where in GENERATED_DIRS else
                "knowledge" if low.endswith(KNOWLEDGE_EXT) else None)
        if kind is None:
            continue
        try:
            size = os.path.getsize(path)
            digest = sha256_file(path) if kind == "knowledge" else None
        except OSError:
            continue
        out.append({"path": os.path.relpath(path, v0_dir), "name": f, "kind": kind, "bytes": size,
                    "sha256": digest, **(parse_name(f) if kind == "knowledge" else {})})
    return out


def read_chroma(chroma_dir: str, collection: str = "curriculum_db") -> dict[str, Any]:
    """Counts per document from chroma.sqlite3, read-only, without chromadb.
    The vectors themselves are not read: they are not migrated."""
    db = os.path.join(chroma_dir, "chroma.sqlite3")
    if not os.path.isfile(db):
        return {"present": False, "path": db}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = {r["name"] for r in con.execute("PRAGMA table_info(collections)")}
    coll = con.execute(f"SELECT id, name{', dimension' if 'dimension' in cols else ''} FROM collections").fetchall()
    target = next((c for c in coll if c["name"] == collection), None)
    rep: dict[str, Any] = {"present": True, "path": db, "collections": [c["name"] for c in coll],
                           "collection": collection, "found": target is not None}
    if target is None:
        return rep
    rep["dimension"] = target["dimension"] if "dimension" in cols else None
    seg = [r["id"] for r in con.execute("SELECT id FROM segments WHERE collection = ?", (target["id"],))]
    qm = ",".join("?" * len(seg)) or "''"
    ids = [r["id"] for r in con.execute(f"SELECT id FROM embeddings WHERE segment_id IN ({qm})", seg)]
    rep["chunks"] = len(ids)
    meta: dict[int, dict[str, Any]] = collections.defaultdict(dict)
    text: dict[int, str] = {}
    for r in con.execute(f"SELECT m.id, m.key, m.string_value, m.int_value FROM embedding_metadata m "
                         f"JOIN embeddings e ON e.id = m.id WHERE e.segment_id IN ({qm})", seg):
        if r["key"] == "chroma:document":
            text[r["id"]] = r["string_value"] or ""
        else:
            meta[r["id"]][r["key"]] = r["string_value"] if r["string_value"] is not None else r["int_value"]
    docs: dict[str, dict[str, Any]] = {}
    for cid, m in meta.items():
        name = os.path.basename(str(m.get("file") or m.get("source") or "(no file)"))
        d = docs.setdefault(name, {"name": name, "chunks": 0, "v0_doctype": m.get("doctype"),
                                   "subject": m.get("subject"), "grade": m.get("grade"),
                                   "course": m.get("course") or None})
        d["chunks"] += 1
        d.setdefault("_texts", []).append(text.get(cid, ""))
    for d in docs.values():   # two longest chunks: enough to recognise the document in v1
        d["samples"] = sorted({t for t in d.pop("_texts") if t}, key=len, reverse=True)[:2]
    rep["documents"] = sorted(docs.values(), key=lambda d: d["name"])
    con.close()
    return rep


def v1_sources(url: str) -> dict[str, dict[str, Any]]:
    """checksum -> v1 source summary, from a running Knowledge Service."""
    import httpx

    from v1.identity import tokens
    h = {"Authorization": "Bearer " + tokens.issue_token("eval:v0-inventory", ["platform_admin"], "user", 900)}
    base = url.rstrip("/")
    out: dict[str, dict[str, Any]] = {}
    with httpx.Client(timeout=60) as c:
        for s in c.get(base + "/v1/knowledge/sources", headers=h).json().get("sources", []):
            full = c.get(base + f"/v1/knowledge/sources/{s['version_id']}", headers=h).json().get("source") or {}
            md = full.get("metadata") or {}
            if md.get("content_checksum"):
                out[md["content_checksum"]] = {"version_id": s["version_id"], "source_id": s["source_id"],
                                               "title": s.get("title"), "source_type": s.get("source_type"),
                                               "file_name": md.get("file_name")}
    return out


def _shingles(t: str, n: int = 5) -> set[str]:
    w = re.findall(r"[a-z0-9]+", (t or "").lower())
    return {" ".join(w[i:i + n]) for i in range(max(1, len(w) - n + 1))} if w else set()


def overlap(a: str, b: str) -> float:
    """Share of the shorter text's 5-word shingles found in the other: v0 and
    v1 split documents at different places, so containment, not Jaccard."""
    x, y = _shingles(a), _shingles(b)
    return len(x & y) / min(len(x), len(y)) if x and y else 0.0


MATCH = 0.6


def match_in_v1(url: str, chroma: dict[str, Any], on_disk: set[str]) -> dict[str, dict[str, Any]]:
    """For each document that exists only inside the v0 index: does v1 already
    hold the same text? Asks v1 with the document's own text and compares the
    top passages word for word."""
    import httpx

    from v1.identity import tokens
    h = {"Authorization": "Bearer " + tokens.issue_token("eval:v0-inventory", ["platform_admin"], "user", 3600)}
    out: dict[str, dict[str, Any]] = {}
    with httpx.Client(timeout=120) as c:
        for d in chroma.get("documents", []):
            if d["name"] in on_disk or not d.get("samples"):
                continue
            best = {"overlap": 0.0}
            for sample in d["samples"]:
                q = " ".join(sample.split()[:120])
                res = c.post(url.rstrip("/") + "/v1/knowledge/query", headers=h,
                             json={"query": q, "top_k": 5}).json().get("results", [])
                for r in res:
                    o = overlap(sample, r.get("text") or "")
                    if o > best["overlap"]:
                        md = r.get("metadata") or {}
                        best = {"overlap": round(o, 2), "title": md.get("title"), "source_id": md.get("source_id"),
                                "source_type": md.get("source_type"), "lms_course": md.get("lms_course_id")}
            out[d["name"]] = best
    return out


def decide(files: list[dict[str, Any]], chroma: dict[str, Any], v1: dict[str, dict[str, Any]] | None,
           matches: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    on_disk = {f["name"] for f in files}
    for f in sorted(files, key=lambda x: x["path"]):
        r = {"item": f["path"], "kind": "file", "bytes": f["bytes"], "sha256": f.get("sha256"),
             "v1_mapping": f.get("v1")}
        if f["kind"] == "learner":
            r.update(decision="excluded", reason="learner data (chat attachments or user memory): not knowledge")
        elif f["kind"] == "generated":
            r.update(decision="excluded", reason="generated by v0 (answer PDF or image): not a curriculum source")
        elif f["sha256"] in seen:
            r.update(decision="deprecated", reason=f"duplicate: identical bytes to {seen[f['sha256']]}")
        elif v1 is not None and f["sha256"] in v1:
            s = v1[f["sha256"]]
            r.update(decision="retained", reason=f"already a governed v1 source, reprocessed from the same file "
                                                  f"({s['source_id']}, {s['version_id']})", v1_source=s)
        elif f.get("v1") is None:
            guess = ", ".join(f"{k} {f[k]}" for k in ("subject", "grade", "v0_doctype") if f.get(k)) or "nothing"
            r.update(decision="review", reason=f"v1 cannot read subject and document type from the file name "
                                               f"(v0 guessed: {guess}); a curriculum officer must classify it "
                                               f"before ingestion")
        else:
            r.update(decision="reprocess" if v1 is not None else "reprocess (v1 not checked)",
                     reason="curriculum file not in v1: ingest from this file with the v1 metadata shown, "
                            "after approval")
        if f.get("sha256"):
            seen.setdefault(f["sha256"], f["path"])
        rows.append(r)
    if chroma.get("found"):
        rows.append({"item": f"{chroma['path']} :: {chroma['collection']}", "kind": "vector_index",
                     "chunks": chroma.get("chunks"), "decision": "deprecated",
                     "reason": "v0 vectors: v0 chunking, no source version, checksum, approval, scope or access; "
                               "every source it holds is reprocessed or re-synced instead"})
        for d in chroma.get("documents", []):
            if d["name"] in on_disk:
                continue   # covered by the file row
            lms = (d.get("v0_doctype") == "LMS Material") or d.get("course")
            m = (matches or {}).get(d["name"])
            if m and m["overlap"] >= MATCH:
                rows.append({"item": f"chroma:{d['name']}", "kind": "index_only_document", "chunks": d["chunks"],
                             "v0": {k: d.get(k) for k in ("v0_doctype", "subject", "grade", "course")},
                             "decision": "retained", "v1_match": m,
                             "reason": f"already in v1 with the same text ({m['overlap']:.0%} of its words match): "
                                       f"{m['title']} ({m['source_id']}); v1 holds it from its own source"})
                continue
            rows.append({"item": f"chroma:{d['name']}", "kind": "index_only_document", "chunks": d["chunks"],
                         "v0": {k: d.get(k) for k in ("v0_doctype", "subject", "grade", "course")},
                         **({"v1_match": m} if m else {}),
                         "decision": "resync" if lms else "review",
                         "closest_in_v1": (f"{m['overlap']:.0%} word match with {m.get('title')}" if m and m.get("title")
                                           else None),
                         "reason": ("LMS material held only as v0 chunks: bring it in from the LMS, its "
                                    "authoritative source; if the LMS no longer has it, recover the text for "
                                    "review (not for direct indexing)") if lms else
                                   "in the v0 index but no source file found: origin uncertain, not migrated "
                                   "until a source file is provided"})
    return rows


def markdown(rep: dict[str, Any]) -> str:
    rows = rep["items"]
    by = collections.Counter(r["decision"] for r in rows)
    ch = rep["chroma"]
    L = [f"# NEXA v0 knowledge inventory ({rep['generated_at']})", "",
         f"v0 folder `{rep['v0_dir']}`; v1 checked: {'yes, ' + rep['v1_url'] if rep['v1_url'] else 'no'}.", "",
         f"Files found: {len(rep['files'])}. v0 vector store: "
         + (f"{ch['chunks']} chunks in `{ch['collection']}`, {len(ch.get('documents', []))} documents, "
            f"dimension {ch.get('dimension')}" if ch.get("found") else
            f"{ch.get('error') or ('no collection ' + ch.get('collection', '')) if ch.get('present') else 'not found at ' + ch['path']}")
         + ".", "", "| Decision | Items |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in sorted(by.items())]
    L += ["", "| Item | Kind | Decision | v1 mapping | Reason |", "|---|---|---|---|---|"]
    for r in rows:
        m = r.get("v1_mapping")
        mp = (f"{m['source_type']} (tier {m['authority_tier']}, {m['access_classification']}), "
              f"{m['subject']} {m['grade_levels']} T{m['term'] or '-'}") if m else ""
        size = f", {r['chunks']} chunks" if r.get("chunks") else ""
        why = r["reason"] + (f" Closest in v1: {r['closest_in_v1']}." if r.get("closest_in_v1") else "")
        L.append(f"| {r['item']} | {r['kind']}{size} | {r['decision']} | {mp} | {why} |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--v0-dir", default="", help="the v0 folder (where its main.py and PDFs are)")
    ap.add_argument("--from-report", default="", help="re-decide an earlier inventory (e.g. one taken on a "
                                                       "school server) against --v1-url, without the v0 folder")
    ap.add_argument("--chroma", default="", help="default: <v0-dir>/chroma_store, else ~/Nexa/chroma_store "
                                                 "(v0's fixed CHROMA_DIR)")
    ap.add_argument("--collection", default="curriculum_db")
    ap.add_argument("--v1-url", default="", help="a v1 Knowledge Service to compare against (needs "
                                                 "NEXA_IDENTITY_SECRET)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.from_report:
        with open(os.path.expanduser(args.from_report)) as fh:
            prev = json.load(fh)
        v0, files, chroma = prev["v0_dir"] + f" (from {os.path.basename(args.from_report)})", prev["files"], prev["chroma"]
    elif not args.v0_dir:
        ap.error("--v0-dir or --from-report is required")
    else:
        v0, files, chroma = os.path.abspath(os.path.expanduser(args.v0_dir)), None, None
    if files is None:
        files = scan_files(v0)
        chroma_dir = os.path.expanduser(args.chroma) if args.chroma else next(
            (d for d in (os.path.join(v0, "chroma_store"), os.path.expanduser("~/Nexa/chroma_store"))
             if os.path.isfile(os.path.join(d, "chroma.sqlite3"))), os.path.join(v0, "chroma_store"))
        try:
            chroma = read_chroma(chroma_dir, args.collection)
        except sqlite3.Error as exc:   # a Chroma version with another schema: say so, do not guess
            chroma = {"present": True, "found": False, "path": chroma_dir, "collection": args.collection,
                      "error": f"could not read the Chroma database: {exc}"}
    v1 = v1_sources(args.v1_url) if args.v1_url else None
    matches = (match_in_v1(args.v1_url, chroma, {f["name"] for f in files})
               if args.v1_url and chroma.get("found") else None)
    rep = {"tool": "v0_inventory", "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "v0_dir": v0, "v1_url": args.v1_url or None, "files": files,
           "chroma": {k: v for k, v in chroma.items()}, "items": decide(files, chroma, v1, matches)}
    os.makedirs(args.out, exist_ok=True)
    stem = os.path.join(args.out, f"nexa-v0-inventory-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}")
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
