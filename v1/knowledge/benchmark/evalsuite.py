"""NEXA-M2-008 evaluation suite: dataset, metrics, regression gate, fixture suite.

The suite approves changes to ingestion, chunking, embeddings, retrieval and
reranking. It has three tiers, all reading one versioned dataset
(benchmark/eval/dataset.v<x>.json) and one versioned threshold file
(benchmark/eval/thresholds.v<x>.json):

- fixture: a synthetic corpus and deterministic stand-ins for the embedding
  and chat models, run in-process. It covers behaviour the curriculum corpus
  cannot exercise on demand (authority conflicts, school vs national scope,
  access classes, citation integrity, failure behaviour). Runs in CI on every
  pull request; every check must pass.
- corpus: the PNG STEM curriculum corpus (NEXA-M2-002) with the real models
  through the Model Gateway, in a scratch schema, so it evaluates the candidate
  code before it is deployed. Records the v0 baseline alongside v1. This is the
  release gate (v1/deploy/deploy.sh).
- live: the same corpus questions against a running Knowledge Service over
  HTTP (DEV/QA), including grounded answers, which need the chat model.

Metrics:
- retrieval: recall@1/5/10, precision@5 (share of the top 5 from an expected
  source), MRR@10, nDCG@10 (single relevant item), by question type, subject,
  grade, term, language and format;
- authority correctness: questions whose answer is defined by the syllabus get
  an official syllabus passage first; fixture conflicts resolve to the official
  source;
- no-answer behaviour: evidence graded none on unanswerable questions (correct
  refusal) and not none on answerable ones (false refusal), using the same
  grading as the grounded answer (grounding.assess);
- grounded answers (live, or corpus with --answers): answered, cites a passage
  from an expected source, covers the key terms; no fabricated citation;
- latency p50/p95; failure behaviour (fixture).
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
from collections import defaultdict
from typing import Any, Callable, Iterable, Optional
from urllib.parse import unquote

HERE = os.path.join(os.path.dirname(__file__), "eval")
DEFAULT_DATASET = os.path.join(HERE, "dataset.v1.2.0.json")
DEFAULT_THRESHOLDS = os.path.join(HERE, "thresholds.v1.3.0.json")
TYPES = {"exact_fact", "conceptual", "ambiguous", "multilingual", "no_answer", "conflict", "scope", "access",
         "filters", "citation_integrity", "failure", "general", "spelling", "confidence"}
_W = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "by", "is", "are", "be", "for", "with", "at", "as",
         "it", "its", "from", "that", "this", "can", "must", "only"}


# ---------------------------------------------------------------- dataset

def load_dataset(path: str = DEFAULT_DATASET) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        ds = json.load(fh)
    validate_dataset(ds)
    return ds


def validate_dataset(ds: dict[str, Any]) -> None:
    errs = []
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(ds.get("version", ""))):
        errs.append("version must be semver")
    ids = set()
    fixture_docs = {d["id"] for d in ds.get("fixture_corpus", [])}
    for it in ds.get("items", []):
        i = it.get("id")
        if i in ids:
            errs.append(f"duplicate id {i}")
        ids.add(i)
        if it.get("suite") not in ("corpus", "fixture"):
            errs.append(f"{i}: suite must be corpus or fixture")
        if it.get("type") not in TYPES:
            errs.append(f"{i}: unknown type {it.get('type')}")
        if not it.get("question"):
            errs.append(f"{i}: question missing")
        exp = it.get("expect") or {}
        if it.get("suite") == "corpus" and it.get("type") != "no_answer" and not (it.get("files") and it.get("evidence")):
            errs.append(f"{i}: answerable corpus items need files and evidence")
        for k in ("relevant", "must_include", "must_exclude", "only"):
            for d in exp.get(k, []):
                if d not in fixture_docs:
                    errs.append(f"{i}: {k} refers to unknown fixture document {d}")
    if errs:
        raise ValueError("invalid evaluation dataset: " + "; ".join(errs[:10]))


def norm(s: str) -> str:
    return " ".join(_W.findall((s or "").lower()))


def key_terms(phrases: Iterable[str]) -> list[str]:
    out: list[str] = []
    for p in phrases:
        out += [w for w in _W.findall(p.lower()) if w not in _STOP and len(w) > 1]
    return list(dict.fromkeys(out))


def file_of_result(r: dict[str, Any]) -> Optional[str]:
    """The source file a result came from: metadata file name, else the last
    path segment of a repo:// source URI."""
    md = r.get("metadata") or {}
    if md.get("file_name"):
        return md["file_name"]
    uri = (r.get("provenance") or {}).get("source_uri") or md.get("source_uri") or ""
    if uri.startswith("repo://"):
        return unquote(uri.rsplit("/", 1)[-1])
    return None


def hit_rank(results: list[dict[str, Any]], item: dict[str, Any],
             file_of: Callable[[dict[str, Any]], Optional[str]] = file_of_result) -> Optional[int]:
    ev = [norm(e) for e in item["evidence"]]
    for i, r in enumerate(results, 1):
        if file_of(r) in item["files"] and all(e in norm(r.get("text", "")) for e in ev):
            return i
    return None


def precision_at(results: list[dict[str, Any]], item: dict[str, Any], k: int = 5,
                 file_of: Callable[[dict[str, Any]], Optional[str]] = file_of_result) -> float:
    top = results[:k]
    if not top:
        return 0.0
    return sum(file_of(r) in item["files"] for r in top) / len(top)


# ---------------------------------------------------------------- metrics

def retrieval_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """rows: {rank, precision@5} per answerable item."""
    n = len(rows)
    if not n:
        return {"n": 0}
    ranks = [r["rank"] for r in rows]
    return {
        "n": n,
        "recall@1": round(100 * sum(1 for x in ranks if x and x <= 1) / n, 1),
        "recall@5": round(100 * sum(1 for x in ranks if x and x <= 5) / n, 1),
        "recall@10": round(100 * sum(1 for x in ranks if x and x <= 10) / n, 1),
        "mrr@10": round(sum(1 / x for x in ranks if x and x <= 10) / n, 3),
        "ndcg@10": round(sum(1 / math.log2(x + 1) for x in ranks if x and x <= 10) / n, 3),
        "precision@5": round(statistics.mean(r["precision@5"] for r in rows), 3),
    }


def sliced(rows: list[dict[str, Any]], keys: Iterable[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for k in keys:
        groups: dict[str, list] = defaultdict(list)
        for r in rows:
            groups[f"{k}={r.get(k)}"].append(r)
        for g, rs in sorted(groups.items()):
            out[g] = retrieval_summary(rs)
    return out


def pct(xs: list[float], p: float) -> Optional[float]:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return round(xs[min(len(xs) - 1, max(0, math.ceil(p * len(xs)) - 1))], 1)


def answer_correct(ans: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """Grounded-answer accuracy for one answerable item."""
    used = [c for c in ans.get("citations", []) if c.get("used")]
    files = {file_of_result({"provenance": c.get("provenance")}) for c in used}
    keys = item.get("expect", {}).get("answer_keys") or key_terms(item.get("evidence", []))
    text = norm(ans.get("answer") or "")
    cov = (sum(norm(k) in text for k in keys) / len(keys)) if keys else 1.0
    answered = ans.get("status") in ("grounded", "grounded_weak")
    cites_relevant = bool(files & set(item.get("files", [])))
    return {"answered": answered, "cites_relevant": cites_relevant, "key_coverage": round(cov, 2),
            "correct": answered and cites_relevant and cov >= 0.5,
            "fabricated": len((ans.get("integrity") or {}).get("fabricated_markers", [])),
            "valid_citations": len((ans.get("integrity") or {}).get("valid_markers", []))}


# ---------------------------------------------------------------- scoring

ANSWERABLE = ("exact_fact", "conceptual", "ambiguous", "multilingual", "spelling")
GATING_TYPES = ("exact_fact", "conceptual", "ambiguous")
SLICES = ("type", "subject", "grade", "term", "language", "format", "source_type")


def score_corpus(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Metrics for the corpus and live suites.

    observations: one per corpus item, {item, results, source_types (per
    result), evidence ('strong'|'weak'|'none'|None), ms, answer (optional
    /answer response)}. results carry text and enough to find the file
    (file_of_result), or a 'file' key set by the caller.
    """
    rows, per_item = [], []
    refusals = {"unanswerable": 0, "refused": 0, "answerable": 0, "false_refusals": 0}
    authority = []
    answers = []
    lat = [o["ms"] for o in observations if o.get("ms") is not None]
    fof = (lambda r: r.get("file") or file_of_result(r))
    for o in observations:
        it, res = o["item"], o["results"]
        rec: dict[str, Any] = {"id": it["id"], "type": it["type"], "evidence": o.get("evidence"),
                               "ms": o.get("ms")}
        if it["type"] == "no_answer":
            refusals["unanswerable"] += 1
            if o.get("evidence") == "none":
                refusals["refused"] += 1
            rec["refused"] = o.get("evidence") == "none"
            if o.get("answer") is not None:
                rec["answer_status"] = o["answer"].get("status")
        else:
            rank = hit_rank(res, it, fof)
            p5 = precision_at(res, it, 5, fof)
            rec.update({"rank": rank, "precision@5": round(p5, 2),
                        "top": (fof(res[0]) if res else None)})
            if it.get("approved", True):
                rows.append({**{k: it.get(k) for k in SLICES}, "rank": rank, "precision@5": p5,
                             "gating": it["type"] in GATING_TYPES})
                if o.get("evidence") is not None:
                    refusals["answerable"] += 1
                    if o["evidence"] == "none":
                        refusals["false_refusals"] += 1
            else:
                rows.append({**{k: it.get(k) for k in SLICES}, "rank": rank, "precision@5": p5,
                             "gating": False, "unapproved": True})
            want = (it.get("expect") or {}).get("top_source_type")
            if want and it.get("approved", True):
                got = (o.get("source_types") or [None])[0]
                authority.append(got == want)
                rec["top_source_type"] = got
            if o.get("answer") is not None and it.get("approved", True):
                a = answer_correct(o["answer"], it)
                a["status"] = o["answer"].get("status")
                answers.append(a)
                rec["answer"] = a
        per_item.append(rec)
    gating = [r for r in rows if r["gating"]]
    by_type = {t: retrieval_summary([r for r in rows if r["type"] == t and not r.get("unapproved")])
               for t in ANSWERABLE}
    by_type["multilingual_unapproved"] = retrieval_summary([r for r in rows if r.get("unapproved")])
    m: dict[str, Any] = {
        "retrieval": {"gating": retrieval_summary(gating), "by_type": by_type,
                      "slices": sliced([r for r in rows if not r.get("unapproved")],
                                       ("subject", "grade", "term", "language", "format", "source_type"))},
        "authority": {"n": len(authority),
                      "syllabus_first_rate": round(sum(authority) / len(authority), 3) if authority else None},
        "latency": {"p50_ms": pct(lat, .5), "p95_ms": pct(lat, .95), "n": len(lat)},
    }
    if refusals["unanswerable"] and any(o.get("evidence") is not None for o in observations):
        m["no_answer"] = {
            "n_unanswerable": refusals["unanswerable"], "n_answerable": refusals["answerable"],
            "correct_refusal_rate": round(refusals["refused"] / refusals["unanswerable"], 3),
            "false_refusal_rate": round(refusals["false_refusals"] / max(refusals["answerable"], 1), 3)}
        # End to end (with answers): after the evidence grade, the answer model
        # also checks the passages (INSUFFICIENT_EVIDENCE). What matters is that
        # an unanswerable question never gets a curriculum (grounded) answer; a
        # labelled general answer or the refusal are both correct.
        ua = [r for r in per_item if r["type"] == "no_answer" and r.get("answer_status")]
        if ua:
            cur = sum(r["answer_status"] in ("grounded", "grounded_weak") for r in ua)
            m["no_answer"]["n_answered_e2e"] = len(ua)
            m["no_answer"]["curriculum_answer_rate"] = round(cur / len(ua), 3)
            m["no_answer"]["end_to_end_refusal_rate"] = round(1 - cur / len(ua), 3)
    if answers:
        n = len(answers)
        m["answers"] = {
            "n": n, "accuracy": round(sum(a["correct"] for a in answers) / n, 3),
            "answered_rate": round(sum(a["answered"] for a in answers) / n, 3),
            "cites_relevant_rate": round(sum(a["cites_relevant"] for a in answers) / n, 3),
            "mean_key_coverage": round(statistics.mean(a["key_coverage"] for a in answers), 3),
            "fabricated_citations": sum(a["fabricated"] for a in answers),
            "citation_valid_rate": round(sum(1 for a in answers if a["answered"] and a["valid_citations"] > 0)
                                         / max(sum(a["answered"] for a in answers), 1), 3),
            "ungrounded_rate": round(sum(a["status"] == "ungrounded" for a in answers) / n, 3),
            "statuses": dict(sorted(_count(a["status"] for a in answers).items())),
        }
    return {"metrics": m, "items": per_item}


def _count(xs: Iterable[Any]) -> dict[Any, int]:
    out: dict[Any, int] = defaultdict(int)
    for x in xs:
        out[x] += 1
    return dict(out)


# ---------------------------------------------------------------- gate

def _get(d: dict[str, Any], dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d


def evaluate_gate(report: dict[str, Any], thresholds: dict[str, Any],
                  baseline: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Compare a report with the suite's thresholds (absolute) and, if given, a
    baseline report (maximum allowed drop). Returns {passed, checks}."""
    sections = [report["suite"]]
    if report["suite"] == "corpus" and "answers" in report.get("metrics", {}):
        sections.append("corpus_answers")
    spec: dict[str, dict] = {"min": {}, "max": {}, "max_drop_vs_baseline": {}}
    for sec in sections:
        for k in spec:
            spec[k].update(thresholds.get(sec, {}).get(k, {}))
    checks = []

    def check(name, value, op, limit, gating=True):
        ok = value is not None and (value >= limit if op == ">=" else value <= limit)
        checks.append({"metric": name, "value": value, "op": op, "limit": limit, "passed": ok, "gating": gating})

    for metric, rule in spec.get("min", {}).items():
        check(metric, _get(report["metrics"], metric), ">=", rule)
    for metric, rule in spec.get("max", {}).items():
        check(metric, _get(report["metrics"], metric), "<=", rule)
    if baseline is not None:
        for metric, drop in spec.get("max_drop_vs_baseline", {}).items():
            base, now = _get(baseline.get("metrics", {}), metric), _get(report["metrics"], metric)
            if base is None or now is None:
                continue
            check(f"{metric} vs baseline {base}", now, ">=", round(base - drop, 3))
    passed = all(c["passed"] for c in checks if c["gating"])
    return {"thresholds_version": thresholds.get("version"), "passed": passed, "checks": checks}


# ---------------------------------------------------------------- fixture suite

def run_fixture_suite(ds: dict[str, Any]) -> dict[str, Any]:
    """Run the fixture items in-process against the Knowledge Service app with
    an in-memory store, a bag-of-words stand-in embedder and a scripted chat
    model. Deterministic; no network."""
    os.environ.setdefault("NEXA_IDENTITY_SECRET", "eval-fixture-secret-not-real-0123456789")
    os.environ.setdefault("NEXA_KNOWLEDGE_GATEWAY_TOKEN", "eval-fixture-token")
    import asyncio
    import hashlib
    import time

    from fastapi.testclient import TestClient

    from v1.identity import tokens
    from v1.knowledge import config, embeddings, grounding
    from v1.knowledge import main as km
    from v1.knowledge.embeddings import EmbeddingError
    from v1.knowledge.indexing import IndexManager
    from v1.knowledge.ingestion.pipeline import Pipeline
    from v1.knowledge.store import IndexRecord, InMemoryStore

    dim = 256

    def bow(text: str) -> list[float]:
        v = [0.0] * dim
        for w in re.findall(r"[a-z]{3,}", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    state = {"fault": None, "chat": "honest"}

    async def gateway(texts, model, trace_id=None):
        if state["fault"] == "embedding_down":
            raise EmbeddingError("gateway 503")
        version = "other-model" if state["fault"] == "model_changed" else "nomic-embed-text"
        return [bow(t) for t in texts], {"model_version": version, "runtime": "ollama"}

    async def chat(messages, trace_id=None):
        if state["fault"] == "chat_down":
            raise RuntimeError("gateway 503")
        src = messages[1]["content"]
        m = re.search(r"\[S1\][^\n]*\n(.+)", src)
        first = (m.group(1) if m else "")[:200].rstrip(".")
        if state["chat"] == "fabricated_only":
            return "Inductive charging uses magnets [S9]. See also [4].", "fixture-chat"
        if state["chat"] == "mixed":
            return first + ". [S1][S9]", "fixture-chat"
        if state["chat"] == "hedged":
            return first + ". [S1] There isn't much information about this in the sources [S1].", "fixture-chat"
        if state["chat"] == "uncited_tail":
            return (first + ". [S1]\n\nTo stay healthy, students should sleep under insecticide treated mosquito "
                    "nets and visit the aid post when feverish."), "fixture-chat"
        if state["chat"] == "ref_line":
            return first + ". [S1]\n\n[S1] Fixture Syllabus, Grade 12, Term 2, p. 3", "fixture-chat"
        return first + ". [S1]", "fixture-chat"

    from v1.knowledge import general

    async def general_chat(messages, trace_id=None):
        if state["fault"] == "chat_down":
            raise RuntimeError("gateway 503")
        return "Results change every year, so check a current news source. [S1] [2]", "fixture-general"

    async def web_search(q):
        return [{"title": "Fixture page", "url": "https://example.org/fixture", "snippet": "fixture"}]

    from v1.knowledge import outline, spelling
    outline.clear_cache()
    spelling.clear_cache()
    saved = (embeddings._gateway, grounding.chat, km.PIPE, km.INDEX, dict(config.EVIDENCE_THRESHOLDS),
             general.chat, general.web_search, config.GENERAL_ANSWERS, config.GENERAL_ROLES, config.SPELL_MIN_FREQ)
    config.SPELL_MIN_FREQ = 1  # the fixture corpus is small
    embeddings._gateway, grounding.chat = gateway, chat
    general.chat, general.web_search = general_chat, web_search
    config.GENERAL_ROLES = frozenset({"*"})
    config.EVIDENCE_THRESHOLDS = {"default": {"min": 0.30, "strong": 0.45}}
    store = InMemoryStore()
    pipe = Pipeline()
    km.PIPE, km.INDEX = pipe, IndexManager(store, pipe.registry)
    base_md = {"approval_status": "approved", "access_classification": "learner", "effective_from": "2024-01-01",
               "language": "en"}

    async def load():
        await store.create_index(IndexRecord("idx_eval_fixture", "nexa-embed-default", "nomic-embed-text", "ollama",
                                             dim, "nomic-embed"))
        await store.set_active("idx_eval_fixture")
        await store.upsert("idx_eval_fixture", [
            {"chunk_id": d["id"], "version_id": "v-" + d["id"], "text": d["text"],
             "vector": bow("search_document: " + d["text"]),
             "metadata": {**base_md, **d["metadata"], "chunk_id": d["id"], "citation": d["metadata"]["title"]}}
            for d in ds["fixture_corpus"]])
    asyncio.run(load())

    results = []
    try:
        with TestClient(km.app) as c:
            for it in [i for i in ds["items"] if i["suite"] == "fixture"]:
                p = it.get("principal", {"roles": ["student"]})
                extra = {"school_id": p["school_id"]} if p.get("school_id") else None
                h = {"Authorization": "Bearer " + tokens.issue_token("eval:" + it["id"], p["roles"], "user", 300,
                                                                    extra=extra)}
                exp = it.get("expect", {})
                state["fault"], state["chat"] = it.get("fault"), it.get("chat", "honest")
                config.GENERAL_ANSWERS = it.get("general_mode", "off")
                if state["fault"] == "lexical_unavailable":
                    store.lexical_available = lambda index_id: False
                fails: list[str] = []
                t0 = time.time()
                body = {"query": it["question"], "top_k": 10, "explain": True}
                if it.get("filters"):
                    body["filters"] = it["filters"]
                r = c.post("/v1/knowledge/query", headers=h, json=body)
                ids: list[str] = []
                if "http_status" in exp:
                    if r.status_code != exp["http_status"]:
                        fails.append(f"expected HTTP {exp['http_status']}, got {r.status_code}")
                elif r.status_code != 200:
                    fails.append(f"query HTTP {r.status_code}")
                else:
                    q = r.json()
                    ids = [x["chunk_id"] for x in q["results"]]
                    if exp.get("top") and (not ids or ids[0] != exp["top"]):
                        fails.append(f"top should be {exp['top']}, got {ids[:1]}")
                    for d in exp.get("must_include", []):
                        if d not in ids:
                            fails.append(f"{d} missing")
                    for d in exp.get("must_exclude", []):
                        if d in ids:
                            fails.append(f"{d} must not be visible")
                    if exp.get("only") and set(ids) - set(exp["only"]):
                        fails.append(f"only {exp['only']} allowed, got {ids}")
                    if exp.get("authoritative_first"):
                        a, b = exp["authoritative_first"], exp["above"]
                        if a not in ids or b not in ids or ids.index(a) > ids.index(b):
                            fails.append(f"{a} must rank above {b}: {ids}")
                    if exp.get("conflict_recorded") and not q["retrieval"].get("conflicts"):
                        fails.append("conflict not recorded")
                    if exp.get("after_all_internal"):
                        e = exp["after_all_internal"]
                        tiers = {x["chunk_id"]: (x.get("metadata") or {}).get("authority_tier") for x in q["results"]}
                        if e in ids and any(tiers.get(o, 6) < 6 for o in ids[ids.index(e) + 1:]):
                            fails.append(f"{e} (external) ranked above a curriculum source: {ids}")
                    if exp.get("degraded") and "degraded" not in q["retrieval"]:
                        fails.append("degradation not reported")
                    rel = exp.get("relevant", [])
                    if rel:
                        rank = next((i for i, x in enumerate(ids, 1) if x in rel), None)
                        if rank is None or rank > 5:
                            fails.append(f"no relevant passage in top 5: {ids[:5]}")
                beh = exp.get("behavior")
                if beh and r.status_code == 200:
                    a = c.post("/v1/knowledge/answer", headers=h, json={"question": it["question"]}).json()
                    want = {"answer": ("grounded", "grounded_weak")}.get(beh, (beh,))
                    if a.get("status") not in want:
                        fails.append(f"answer status {a.get('status')}, expected {beh}")
                    txt = a.get("answer") or ""
                    if exp.get("status") and a.get("status") != exp["status"]:
                        fails.append(f"status {a.get('status')}, expected {exp['status']}")
                    if exp.get("confidence") and a.get("confidence") != exp["confidence"]:
                        fails.append(f"confidence {a.get('confidence')}, expected {exp['confidence']}")
                    if "spelling_corrected" in exp and (a.get("spelling") or {}).get("corrections") != \
                            exp["spelling_corrected"]:
                        fails.append(f"spelling {a.get('spelling')}, expected {exp['spelling_corrected']}")
                    if exp.get("spelling_choices") and set(((a.get("spelling") or {}).get("ambiguous") or {})
                                                           .get(exp["spelling_choices"][0], [])) != \
                            set(exp["spelling_choices"][1:]):
                        fails.append(f"spelling choices {a.get('spelling')}, expected {exp['spelling_choices']}")
                    if "general_label" in exp and (grounding.GENERAL_LABEL in txt) != exp["general_label"]:
                        fails.append("general-information label " + ("missing" if exp["general_label"] else "present"))
                    for bad in exp.get("answer_excludes", []):
                        if bad in txt:
                            fails.append(f"answer still contains {bad!r}")
                    if a.get("status") == "general":
                        txt = a.get("answer") or ""
                        if not txt.startswith(("Not from your approved curriculum materials",)):
                            fails.append("general answer is not labelled as outside the curriculum")
                        if a.get("citations") or a.get("source_kind") == "curriculum":
                            fails.append("general answer carries curriculum citations")
                        if "[S1]" in txt or "[2]" in txt:
                            fails.append("invented marker left in a general answer")
                        if exp.get("source_kind") and a.get("source_kind") != exp["source_kind"]:
                            fails.append(f"source_kind {a.get('source_kind')}, expected {exp['source_kind']}")
                    if a.get("integrity", {}).get("fabricated_markers") and a.get("status") in ("grounded",
                                                                                                "grounded_weak"):
                        if not exp.get("fabricated_removed"):
                            fails.append("fabricated citations")
                        elif any(m in (a.get("answer") or "") for m in ("[S9]", "[4]")):
                            fails.append("fabricated marker left in answer")
                    if exp.get("citations_listed") and not a.get("citations"):
                        fails.append("passages not listed")
                    for k in exp.get("answer_keys", []):
                        if a.get("status") in ("grounded", "grounded_weak") and k.lower() not in (a.get("answer") or "").lower():
                            fails.append(f"answer lacks '{k}'")
                if state["fault"] == "lexical_unavailable":
                    del store.lexical_available
                results.append({"id": it["id"], "type": it["type"], "passed": not fails, "failures": fails,
                                "ms": round((time.time() - t0) * 1000, 1)})
    finally:
        (embeddings._gateway, grounding.chat, km.PIPE, km.INDEX, eth, general.chat, general.web_search,
         config.GENERAL_ANSWERS, config.GENERAL_ROLES, config.SPELL_MIN_FREQ) = saved
        outline.clear_cache()
        spelling.clear_cache()
        config.EVIDENCE_THRESHOLDS = eth
        state["fault"] = None
    passed = sum(r["passed"] for r in results)
    by_type: dict[str, list] = defaultdict(list)
    for r in results:
        by_type[r["type"]].append(r["passed"])
    return {"suite": "fixture", "dataset_version": ds["version"], "items": results,
            "metrics": {"pass_rate": round(passed / len(results), 3) if results else 0.0,
                        "passed": passed, "total": len(results),
                        "by_type": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_type.items())}}}
