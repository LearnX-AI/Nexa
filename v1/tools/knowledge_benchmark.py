"""NEXA-M2-003 chunking benchmark: v0 strategy against v1 strategy.

    python v1/tools/knowledge_benchmark.py --corpus /tmp/nexa-corpus [--out report.json]

Run knowledge_corpus.py first; this reads its registry (the v1 chunks) and
rebuilds the v0 chunks from the same original files with the exact v0 loaders
and splitter (v1/knowledge/benchmark/v0_baseline.py).

The retriever is held fixed (BM25) so only the chunking strategy differs. The
same gold set can be re-run with dense embeddings through the Model Gateway on
the DGX; that run is recorded separately in the M2-003 report.

Metrics:
- shape: chunk count, token size percentiles
- boundaries: share of chunks that start mid-sentence (lower-case start)
- noise: share of chunks carrying running headers, page labels, letter-spaced
  text, TOC dot leaders or worksheet underscore lines
- retention: share of the source's content words present in the chunk set
  (reference = v1 extraction of each file, which includes DOCX tables)
- table integrity: share of DOCX table rows found whole inside one chunk
- provenance: share of chunks carrying each provenance field
- retrieval: recall@1/3/5 and MRR on the gold set, overall and per category;
  for v1 also body-only (no context header) and with the M2-001 authority
  weights applied to scores, a preview of the retrieval reranking step
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from v1.knowledge.authority import AUTHORITY_WEIGHTS  # noqa: E402
from v1.knowledge.benchmark.bm25 import BM25  # noqa: E402
from v1.knowledge.benchmark.v0_baseline import v0_chunks  # noqa: E402
from v1.knowledge.curriculum import draft_from_filename  # noqa: E402
from v1.knowledge.ingestion.extractors import extract  # noqa: E402
from v1.knowledge.ingestion.pipeline import to_positioned_blocks  # noqa: E402
from v1.knowledge.ingestion.registry import Registry  # noqa: E402
from v1.knowledge.ingestion.validation import sniff  # noqa: E402
from v1.knowledge.metadata import AuthorityTier  # noqa: E402
from v1.knowledge.processing.chunking import count_tokens  # noqa: E402

GOLD = os.path.join(ROOT, "v1", "knowledge", "benchmark", "gold_questions.json")
_NOISE = [
    ("page_label", re.compile(r"\|\s*P\s?a\s?g\s?e\b")),
    ("letter_spaced", re.compile(r"(?:\b\w\s){6,}\w\b")),
    ("toc_leaders", re.compile(r"(?:\.\s?){8,}")),
    ("answer_underscores", re.compile(r"_{10,}")),
]
_WORD = re.compile(r"[a-z0-9]+")


def norm(s: str) -> str:
    return " ".join(_WORD.findall(s.lower()))


def v0_meta(name: str) -> dict:
    """The metadata v0 attached (main.py parse_curriculum_filename / parse_docx_curriculum_filename)."""
    d = draft_from_filename(name)
    kind = {"official_syllabus": "Syllabus", "teacher_guide": "Teacher Guide",
            "student_guide": "Student Guide"}.get(getattr(d.get("source_type"), "value", ""), "Lesson Notes")
    return {"file": name, "subject": getattr(d.get("subject"), "value", "Unknown"),
            "grade": f"Grade {d['grade_levels'][0]}" if d.get("grade_levels") else "Unknown",
            "term": f"Term {d['term']}" if d.get("term") else "Unknown", "doctype": kind}


def pct(a: float, b: float) -> float:
    return round(100.0 * a / b, 1) if b else 0.0


def shape(texts: list[str]) -> dict:
    toks = sorted(count_tokens(t) for t in texts) or [0]
    n = len(toks)
    return {"chunks": len(texts), "tokens_p5": toks[n // 20], "tokens_median": toks[n // 2],
            "tokens_p95": toks[min(n - 1, n * 19 // 20)], "tokens_max": toks[-1],
            "under_30_tokens_pct": pct(sum(t < 30 for t in toks), n)}


def boundaries(texts: list[str]) -> dict:
    starts_mid = sum(1 for t in texts if t.strip()[:1].islower())
    return {"starts_mid_sentence_pct": pct(starts_mid, len(texts))}


def noise(texts: list[str]) -> dict:
    out = {}
    any_noise = 0
    for t in texts:
        hit = False
        for name, rx in _NOISE:
            if rx.search(t):
                out[name] = out.get(name, 0) + 1
                hit = True
        any_noise += hit
    return {"chunks_with_noise_pct": pct(any_noise, len(texts)),
            **{f"{k}_pct": pct(v, len(texts)) for k, v in sorted(out.items())}}


def retention(ref_by_file: dict[str, Counter], chunks_by_file: dict[str, list[str]]) -> dict:
    total = covered = 0
    per_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for f, ref in ref_by_file.items():
        got = Counter(w for t in chunks_by_file.get(f, []) for w in _WORD.findall(t.lower()))
        c = sum(min(n, got[w]) for w, n in ref.items())
        s = sum(ref.values())
        total += s
        covered += c
        kind = os.path.splitext(f)[1].lstrip(".") + ("-scanned" if f.startswith("scanned-") else "")
        per_type[kind][0] += c
        per_type[kind][1] += s
    return {"content_retention_pct": pct(covered, total),
            "by_format": {k: pct(c, s) for k, (c, s) in sorted(per_type.items())}}


def table_integrity(rows_by_file: dict[str, list[str]], chunks_by_file: dict[str, list[str]]) -> dict:
    total = whole = 0
    for f, rows in rows_by_file.items():
        normed = [norm(t) for t in chunks_by_file.get(f, [])]
        for r in rows:
            nr = norm(r)
            if len(nr) < 8:
                continue
            total += 1
            whole += any(nr in c for c in normed)
    return {"docx_table_rows": total, "table_rows_intact_pct": pct(whole, total)}


def provenance(metas: list[dict], fields: dict[str, str]) -> dict:
    return {label: pct(sum(1 for m in metas if m.get(key) not in (None, "", "Unknown", [])), len(metas))
            for label, key in fields.items()}


def retrieval(docs: list[str], files: list[str], gold: list[dict], weights: list[float] | None = None) -> dict:
    bm = BM25(docs)
    normed = [norm(d) for d in docs]

    def search(q: str) -> list[tuple[int, float]]:
        if weights is None:
            return bm.search(q, k=10)
        scored = [(i, s * weights[i]) for i, s in bm.search(q, k=len(docs))]
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:10]

    ranks: list[int | None] = []
    per_cat: dict[str, list[int | None]] = defaultdict(list)
    for q in gold:
        ev = [norm(e) for e in q["evidence"]]
        rank = None
        for r, (i, _) in enumerate(search(q["question"]), start=1):
            if files[i] in q["files"] and all(e in normed[i] for e in ev):
                rank = r
                break
        ranks.append(rank)
        per_cat[q["category"]].append(rank)

    def summarise(rs: list[int | None]) -> dict:
        n = len(rs)
        return {"n": n,
                "recall@1": pct(sum(1 for r in rs if r and r <= 1), n),
                "recall@3": pct(sum(1 for r in rs if r and r <= 3), n),
                "recall@5": pct(sum(1 for r in rs if r and r <= 5), n),
                "mrr@10": round(sum(1.0 / r for r in rs if r) / n, 3) if n else 0.0}

    return {"overall": summarise(ranks), "by_category": {c: summarise(r) for c, r in sorted(per_cat.items())},
            "per_question": {q["id"]: r for q, r in zip(gold, ranks)}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, help="output directory of knowledge_corpus.py")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    reg = Registry(f"sqlite:///{os.path.join(args.corpus, 'registry.db')}")
    gold = json.load(open(GOLD))["questions"]

    # v1 chunks and the file each came from
    v1_texts, v1_files, v1_metas, v1_emb, v1_weights = [], [], [], [], []
    v1_by_file: dict[str, list[str]] = defaultdict(list)
    originals: dict[str, bytes] = {}
    for s in reg.list_sources("active"):
        name = s["metadata"]["file_name"]
        originals[name] = open(os.path.join(args.corpus, "originals", s["metadata"]["content_checksum"][7:9],
                                            s["metadata"]["content_checksum"][7:]), "rb").read()
        for c in reg.chunks_of(s["version_id"]):
            body = c["text"][c["overlap_prefix_chars"]:] if c["overlap_prefix_chars"] else c["text"]
            v1_texts.append(c["text"])
            v1_emb.append(c["embedding_text"])
            v1_files.append(name)
            v1_weights.append(AUTHORITY_WEIGHTS[AuthorityTier(c["source"]["authority_tier"])])
            v1_by_file[name].append(" ".join(c["locator"]["section_path"]) + "\n" + body)
            loc, cur, src = c["locator"], c["curriculum"], c["source"]
            v1_metas.append({"file": name, "subject": src["subject"], "grade": src["grade_levels"],
                             "term_or_module": src.get("term") or cur.get("module_code"),
                             "doctype": src["source_type"], "page": loc.get("page_start"),
                             "printed_page": loc.get("page_label"), "section": loc.get("section_path"),
                             "module": cur.get("module_code"), "topic": cur.get("topic_title"),
                             "outcome_codes": cur.get("outcome_codes"),
                             "version": src.get("version_id"), "authority": src.get("authority_tier"),
                             "access": src.get("access_classification"), "approval": src.get("approval_status"),
                             "chunker_version": c["chunking"]["chunker_version"], "citation": c.get("citation"),
                             "neighbours": c.get("prev_chunk_id") or c.get("next_chunk_id")})

    # v0 chunks from the same originals
    v0_texts, v0_files, v0_metas = [], [], []
    v0_by_file: dict[str, list[str]] = defaultdict(list)
    for name, data in sorted(originals.items()):
        for c in v0_chunks(name, data, v0_meta(name)):
            v0_texts.append(c["text"])
            v0_files.append(name)
            v0_metas.append(c["metadata"])
            v0_by_file[name].append(c["text"])

    # reference content per file (v1 extraction, which includes tables)
    ref_by_file: dict[str, Counter] = {}
    table_rows: dict[str, list[str]] = {}
    for name, data in originals.items():
        doc = extract(sniff(data), data)
        pbs, _ = to_positioned_blocks(doc)
        ref_by_file[name] = Counter(w for pb in pbs for w in _WORD.findall(
            ("\n".join(" | ".join(r) for r in pb.block.rows) if pb.block.rows else pb.block.text).lower()))
        if name.endswith(".docx"):
            table_rows[name] = [" ".join(r) for pb in pbs if pb.block.rows for r in pb.block.rows[1:]]

    # gold evidence must exist in the v1 chunks of an expected file, or the question is invalid
    invalid = [q["id"] for q in gold
               if not any(f in q["files"] and all(norm(e) in norm(t) for e in q["evidence"])
                          for t, f in zip(v1_texts, v1_files))]

    v0_fields = {"file": "file", "subject": "subject", "grade": "grade", "term_or_module": "term",
                 "doctype": "doctype", "page": "page"}
    v1_fields = {k: k for k in ["file", "subject", "grade", "term_or_module", "doctype", "page", "printed_page",
                                "section", "module", "topic", "outcome_codes", "version", "authority", "access",
                                "approval", "chunker_version", "citation", "neighbours"]}
    report = {
        "files": len(originals),
        "gold_questions": len(gold),
        "gold_invalid": invalid,
        "v0": {"strategy": "RecursiveCharacterTextSplitter 1500/300 per PyPDFLoader page; DOCX paragraphs only",
               "files_with_chunks": len(v0_by_file),
               **shape(v0_texts), **boundaries(v0_texts), **noise(v0_texts),
               **retention(ref_by_file, v0_by_file), **table_integrity(table_rows, v0_by_file),
               "provenance": provenance(v0_metas, v0_fields),
               "retrieval": retrieval(v0_texts, v0_files, gold)},
        "v1": {"strategy": "nexa-structural 1.0.0 (target 320, max 512, overlap 48, profiles per subject)",
               "files_with_chunks": len(v1_by_file),
               **shape(v1_texts), **boundaries(v1_texts), **noise(v1_texts),
               **retention(ref_by_file, v1_by_file), **table_integrity(table_rows, v1_by_file),
               "provenance": provenance(v1_metas, v1_fields),
               "retrieval": retrieval(v1_emb, v1_files, gold),
               "retrieval_body_only": retrieval(v1_texts, v1_files, gold),
               "retrieval_with_authority_weights": retrieval(v1_emb, v1_files, gold, v1_weights)},
    }
    out = args.out or os.path.join(args.corpus, "benchmark_report.json")
    with open(out, "w") as fh:
        json.dump(report, fh, indent=2)
    slim = {k: {kk: vv for kk, vv in v.items() if not kk.startswith("retrieval")}
            if isinstance(v, dict) else v for k, v in report.items()}
    print(json.dumps(slim, indent=1))
    for k in ("v0", "v1"):
        print(k, "retrieval", json.dumps(report[k]["retrieval"]["overall"]),
              json.dumps({c: v["recall@5"] for c, v in report[k]["retrieval"]["by_category"].items()}))
    print("v1 body-only retrieval", json.dumps(report["v1"]["retrieval_body_only"]["overall"]))
    aw = report["v1"]["retrieval_with_authority_weights"]
    print("v1 + M2-001 authority weights", json.dumps(aw["overall"]),
          json.dumps({c: v["recall@5"] for c, v in aw["by_category"].items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
