"""NEXA-M2-008 RAG evaluation: fixture, corpus and live suites, and the release gate.

    # CI / any machine: deterministic fixture suite (no models, no network)
    python v1/tools/nexa_eval.py fixture --out OUT

    # DGX: the release gate. Builds the candidate's index of the M2-002 corpus
    # in a scratch schema with the real models, runs v1 and the v0 baseline,
    # compares with the recorded baseline, and records a gate result that
    # v1/deploy/deploy.sh checks before deploying retrieval changes.
    python v1/tools/nexa_eval.py corpus --corpus DIR --pg-dsn DSN --out OUT --record-gate [--answers]

    # DEV/QA: the same questions against a running Knowledge Service
    python v1/tools/nexa_eval.py live --base-url http://127.0.0.1:8103 --out OUT

Exit status 0 when the gate passes, 1 when it fails, 2 on a usage or setup error.
Thresholds come from benchmark/eval/thresholds.v*.json, never from flags.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import time
import uuid

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

from v1.knowledge.benchmark import evalsuite as ev  # noqa: E402

BASELINES = os.path.join(ROOT, "v1", "knowledge", "benchmark", "eval", "baselines")
GATE_PATHS = ("v1/knowledge", "v1/model_gateway")


# ---------------------------------------------------------------- helpers

def _quiet():
    """The services log JSON lines to stdout; keep the report readable."""
    from v1.observability import core
    core.log_event = lambda **f: None
    for name, m in list(sys.modules.items()):
        if name.startswith("v1.") and m is not None and name != "v1.observability.core":
            if getattr(m, "log_event", None) is not None:
                m.log_event = core.log_event
            if getattr(m, "audit_event", None) is not None:
                m.audit_event = lambda *a, **k: None


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", ROOT, *args], check=True, capture_output=True, text=True).stdout.strip()


def gate_key(commit: str = "HEAD", repo: str = ROOT) -> str:
    """Identifies the retrieval code under test: a hash of the tree of
    v1/knowledge and v1/model_gateway at a commit, excluding the evaluation
    data (benchmark/eval), so a threshold or baseline change does not by
    itself need a new run and a merge commit that leaves the code as it was on
    the evaluated branch keeps its result. retrieval_key() in deploy.sh
    computes the same value."""
    out = subprocess.run(["bash", "-c", "git ls-tree -r \"$1\" -- v1/knowledge v1/model_gateway "
                          "| grep -v $'\\tv1/knowledge/benchmark/eval/' | sha1sum | cut -c1-16", "_", commit],
                         cwd=repo, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def worktree_clean() -> bool:
    return not git("status", "--porcelain", "--", *GATE_PATHS, ":(exclude)v1/knowledge/benchmark/eval")


def gate_dir() -> str:
    return os.environ.get("NEXA_EVAL_GATE_DIR") or os.path.expanduser("~/.nexa/eval-gates")


def write_report(report: dict, out: str, stem: str) -> tuple[str, str]:
    os.makedirs(out, exist_ok=True)
    jp, mp = os.path.join(out, stem + ".json"), os.path.join(out, stem + ".md")
    with open(jp, "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    with open(mp, "w") as fh:
        fh.write(markdown(report))
    return jp, mp


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M")


def load_thresholds(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def baseline_path(scheme: str) -> str:
    return os.path.join(BASELINES, f"corpus-{scheme}.json")


# ---------------------------------------------------------------- fixture

def run_fixture(args) -> int:
    os.environ.setdefault("NEXA_IDENTITY_SECRET", "eval-fixture-secret-not-real-0123456789")
    os.environ.setdefault("NEXA_KNOWLEDGE_GATEWAY_TOKEN", "eval-fixture-token")
    import v1.knowledge.main  # noqa: F401  (import before silencing its logs)
    _quiet()
    ds = ev.load_dataset(args.dataset)
    rep = ev.run_fixture_suite(ds)
    rep.update({"generated_at": now(), "commit": _commit()})
    rep["gate"] = ev.evaluate_gate(rep, load_thresholds(args.thresholds))
    jp, mp = write_report(rep, args.out, f"nexa-eval-fixture-{stamp()}")
    print(markdown(rep))
    print(f"report: {mp}\n        {jp}")
    return 0 if rep["gate"]["passed"] else 1


def _commit() -> str | None:
    try:
        return git("rev-parse", "HEAD")
    except Exception:
        return None


# ---------------------------------------------------------------- corpus

async def corpus_async(args) -> dict:
    from v1.identity.principal import Principal
    from v1.knowledge import embeddings, grounding, spelling
    from v1.knowledge.access import retrieval_filter
    from v1.knowledge.indexing import IndexManager, info_of
    from v1.knowledge.ingestion.blobstore import BlobStore
    from v1.knowledge.ingestion.registry import Registry
    from v1.knowledge.pgstore import PgVectorStore
    from v1.knowledge.retrieval import RetrievalRequest, Retriever
    from v1.knowledge.store import InMemoryStore

    from knowledge_retrieval_bench import build_v0

    ds = ev.load_dataset(args.dataset)
    items = [i for i in ds["items"] if i["suite"] == "corpus"]
    if args.offline:
        from knowledge_embed_eval import offline_gateway
        embeddings._gateway = offline_gateway([{"logical": args.model, "ollama_tag": "nomic-embed-text:v1.5"},
                                               {"logical": args.v0_model, "ollama_tag": "nomic-embed-text:v1.5"}])
    if not os.path.exists(os.path.join(args.corpus, "registry.db")):
        print(f"building the M2-002 corpus into {args.corpus} (knowledge_corpus.py) ...", flush=True)
        subprocess.run([sys.executable, os.path.join(ROOT, "v1", "tools", "knowledge_corpus.py"), "--out",
                        args.corpus], check=True, stdout=subprocess.DEVNULL)
    reg = Registry(f"sqlite:///{os.path.join(args.corpus, 'registry.db')}")
    blobs = BlobStore(os.path.join(args.corpus, "originals"))
    files, src_md = {}, {}
    for s in reg.list_sources("active"):
        if s["metadata"].get("approval_status") != "approved":
            continue
        src_md[s["metadata"]["file_name"]] = s["metadata"]
        for c in reg.chunks_of(s["version_id"]):
            files[c["chunk_id"]] = s["metadata"]["file_name"]
    missing = sorted({f for i in items for f in i.get("files", []) if f not in src_md})
    schema = "nexa_eval_" + uuid.uuid4().hex[:8]
    store = PgVectorStore(args.pg_dsn, schema=schema) if args.pg_dsn else InMemoryStore()
    await store.open()
    who = retrieval_filter(Principal("eval", ("platform_admin",), "user"))
    out: dict = {"suite": "corpus", "dataset_version": ds["version"], "generated_at": now(),
                 "commit": _commit(), "gate_key": _safe(gate_key), "worktree_clean": _safe(worktree_clean),
                 "mode": "offline" if args.offline else "gateway", "store": store.name,
                 "corpus": {"sources": len(src_md), "chunks": len(files), "missing_files": missing}}
    try:
        mgr = IndexManager(store, reg)
        t0 = time.time()
        rec = await mgr.start_build(args.model, note="M2-008 evaluation")
        rec = await mgr.run_build(rec.index_id, activate=True)
        out["index"] = {"fingerprint": rec.fingerprint, "prefix_scheme": rec.prefix_scheme,
                        "validation_passed": rec.validation.get("passed"), "build_s": round(time.time() - t0, 1)}
        retr = Retriever(mgr)
        obs = []
        for n, it in enumerate(items, 1):
            chk = await spelling.check(mgr, it["question"])
            rr = await retr.retrieve(RetrievalRequest(chk.corrected or it["question"], 10,
                                                      filters=it.get("context_filters") or {}), who)
            res = [{**r, "file": files.get(r["chunk_id"])} for r in rr.results]
            o = {"item": it, "results": res, "ms": rr.explain["timings_ms"]["total"],
                 "source_types": [(src_md.get(r["file"]) or {}).get("source_type") for r in res],
                 "evidence": grounding.assess(rr).level}
            if args.answers and it.get("approved", True) and it["type"] in ("exact_fact", "conceptual", "ambiguous",
                                                                             "no_answer"):
                a = await grounding.answer(chk.corrected or it["question"], rr, "student")
                ad = a.model_dump(mode="json")
                for c in ad.get("citations", []):
                    f = files.get(c.get("chunk_id"))
                    if f:
                        c.setdefault("provenance", {})["source_uri"] = "repo://" + f
                o["answer"] = ad
            obs.append(o)
            if n % 20 == 0:
                print(f"  v1: {n}/{len(items)}", flush=True)
        scored = ev.score_corpus(obs)
        out["pipeline"] = {"name": "v1", "retrieval": retr.describe() if hasattr(retr, "describe") else None}
        out["metrics"], out["items"] = scored["metrics"], scored["items"]

        if not args.no_v0:
            v0emb = mgr.embedder(args.v0_model)
            v0info = await v0emb.describe()
            v0 = await build_v0(store, reg, blobs, v0emb, v0info)
            obs0 = []
            for it in items:
                t = time.time()
                qv, _ = await v0emb.embed_query(it["question"], expect=info_of(v0))
                rows = await store.query(v0.index_id, qv, 10)
                res = [{**r, "file": (r.get("metadata") or {}).get("file")} for r in rows]
                obs0.append({"item": it, "results": res, "ms": (time.time() - t) * 1000,
                             "source_types": [(src_md.get(r["file"]) or {}).get("source_type") for r in res],
                             "evidence": None})
            s0 = ev.score_corpus(obs0)
            out["v0_baseline"] = {"fingerprint": v0.fingerprint, "metrics": s0["metrics"], "items": s0["items"],
                                  "note": "v0 had no evidence grading, so no-answer behaviour is not measured"}
    finally:
        if store.name == "pgvector":
            async with store._conn() as conn:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await store.close()
    return out


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


def run_corpus(args) -> int:
    import v1.knowledge.indexing  # noqa: F401
    import v1.knowledge.retrieval  # noqa: F401
    _quiet()
    rep = asyncio.run(corpus_async(args))
    th = load_thresholds(args.thresholds)
    scheme = rep["index"]["prefix_scheme"]
    bpath = args.baseline or baseline_path(scheme)
    base = None
    if os.path.exists(bpath) and not args.offline:
        with open(bpath) as fh:
            base = json.load(fh)
        if base.get("index", {}).get("fingerprint") != rep["index"]["fingerprint"]:
            rep["baseline_note"] = (f"baseline {bpath} is for {base.get('index', {}).get('fingerprint')}, "
                                    f"not {rep['index']['fingerprint']}; regression check skipped")
            base = None
    rep["baseline"] = {"path": os.path.relpath(bpath, ROOT), "used": base is not None,
                       "generated_at": (base or {}).get("generated_at"), "commit": (base or {}).get("commit")}
    rep["gate"] = ev.evaluate_gate(rep, th, base)
    if args.offline:
        rep["gate"]["passed"] = False
        rep["gate"]["note"] = "offline stand-in embedders: harness check only, never a release gate"
    if "v0_baseline" in rep:
        g, g0 = rep["metrics"]["retrieval"]["gating"], rep["v0_baseline"]["metrics"]["retrieval"]["gating"]
        rep["v1_vs_v0"] = {k: round(g[k] - g0[k], 3) for k in ("recall@1", "recall@5", "recall@10", "mrr@10",
                                                                "ndcg@10", "precision@5") if k in g and k in g0}
    stem = f"nexa-eval-corpus-{scheme}{'-offline' if args.offline else ''}-{stamp()}"
    jp, mp = write_report(rep, args.out, stem)
    print(markdown(rep))
    print(f"report: {mp}\n        {jp}")
    if args.write_baseline and not args.offline:
        os.makedirs(BASELINES, exist_ok=True)
        slim = {k: rep[k] for k in ("suite", "dataset_version", "generated_at", "commit", "gate_key", "index",
                                    "corpus", "metrics", "v0_baseline") if k in rep}
        slim.get("v0_baseline", {}).pop("items", None)
        with open(bpath, "w") as fh:
            json.dump(slim, fh, indent=2, default=str)
        print(f"baseline written: {bpath} (commit it in a reviewed change)")
    if args.record_gate:
        if args.offline:
            print("not recording a gate result: offline run")
        elif not rep["worktree_clean"]:
            print("not recording a gate result: v1/knowledge or v1/model_gateway has uncommitted changes")
        elif rep["gate"]["passed"]:
            os.makedirs(gate_dir(), exist_ok=True)
            gp = os.path.join(gate_dir(), f"{rep['gate_key']}.json")
            with open(gp, "w") as fh:
                json.dump({"gate_key": rep["gate_key"], "commit": rep["commit"], "passed": True,
                           "generated_at": rep["generated_at"], "fingerprint": rep["index"]["fingerprint"],
                           "thresholds_version": th.get("version"), "dataset_version": rep["dataset_version"],
                           "report": jp}, fh, indent=2)
            print(f"gate PASSED and recorded: {gp}")
        else:
            print("gate FAILED: nothing recorded, deploy.sh will refuse this retrieval change")
    return 0 if rep["gate"]["passed"] else 1


# ---------------------------------------------------------------- live

def run_live(args) -> int:
    import httpx

    from v1.identity import tokens

    ds = ev.load_dataset(args.dataset)
    items = [i for i in ds["items"] if i["suite"] == "corpus"]
    if not os.environ.get("NEXA_IDENTITY_SECRET"):
        print("NEXA_IDENTITY_SECRET is not set: load it first with\n"
              "  set -a; source <(sudo cat /etc/nexa/identity.env); set +a", file=sys.stderr)
        return 2
    tok = tokens.issue_token("eval:nexa-eval-live", ["platform_admin"], "user", 3600)
    h = {"Authorization": f"Bearer {tok}"}
    base = args.base_url.rstrip("/")
    obs = []
    with httpx.Client(timeout=180) as c:
        health = c.get(base + "/health").json()
        for n, it in enumerate(items, 1):
            body = {"query": it["question"], "top_k": 10, "explain": True}
            if it.get("context_filters"):
                body["filters"] = it["context_filters"]
            t = time.time()
            r = c.post(base + "/v1/knowledge/query", headers=h, json=body)
            ms = (time.time() - t) * 1000
            r.raise_for_status()
            q = r.json()
            res = q["results"]
            o = {"item": it, "results": res, "ms": (q.get("retrieval") or {}).get("timings_ms", {}).get("total", ms),
                 "source_types": [(x.get("metadata") or {}).get("source_type") for x in res], "evidence": None}
            if not args.no_answers and (it["type"] == "no_answer"
                                        or (it.get("approved", True) and it["type"] != "multilingual")):
                ab = {"question": it["question"]}
                if it.get("context_filters"):
                    ab["filters"] = it["context_filters"]
                a = c.post(base + "/v1/knowledge/answer", headers=h, json=ab).json()
                o["evidence"] = (a.get("evidence") or {}).get("level")
                o["answer"] = a
            obs.append(o)
            if n % 10 == 0:
                print(f"  live: {n}/{len(items)}", flush=True)
    scored = ev.score_corpus(obs)
    rep = {"suite": "live", "dataset_version": ds["version"], "generated_at": now(), "base_url": base,
           "service": {"version": health.get("version"), "index": (health.get("index") or {})},
           "metrics": scored["metrics"], "items": scored["items"]}
    rep["gate"] = ev.evaluate_gate(rep, load_thresholds(args.thresholds))
    jp, mp = write_report(rep, args.out, f"nexa-eval-live-{stamp()}")
    print(markdown(rep))
    print(f"report: {mp}\n        {jp}")
    return 0 if rep["gate"]["passed"] else 1


# ---------------------------------------------------------------- report

def _fmt(v):
    return "-" if v is None else v


def markdown(r: dict) -> str:
    g = r.get("gate", {})
    L = [f"# NEXA-M2-008 evaluation: {r['suite']} suite", "",
         f"Dataset {r.get('dataset_version')}, thresholds {g.get('thresholds_version')}. Run {r.get('generated_at')}"
         + (f", commit `{(r.get('commit') or '')[:12]}`" if r.get("commit") else "")
         + (f", gate key `{r['gate_key']}`" if r.get("gate_key") else "") + ".", ""]
    if r.get("index"):
        L += [f"Index {r['index']['fingerprint']}, built in {r['index'].get('build_s')} s "
              f"({r['corpus']['sources']} sources, {r['corpus']['chunks']} chunks). Store {r.get('store')}.", ""]
        if r["corpus"].get("missing_files"):
            L += [f"> Files named by the dataset but not in this corpus: {', '.join(r['corpus']['missing_files'])}",
                  ""]
    if r.get("mode") == "offline":
        L += ["> **Offline stand-in embedders.** This checks the harness only; the numbers say nothing about "
              "the real models and can never pass the gate.", ""]
    L += [f"**Gate: {'PASS' if g.get('passed') else 'FAIL'}**" + (f" ({g['note']})" if g.get("note") else ""), "",
          "| Check | Value | Limit | Result |", "|---|---|---|---|"]
    for c in g.get("checks", []):
        L.append(f"| {c['metric']} | {_fmt(c['value'])} | {c['op']} {c['limit']} | "
                 f"{'pass' if c['passed'] else '**FAIL**'} |")
    if r.get("baseline_note"):
        L += ["", f"> {r['baseline_note']}"]
    m = r.get("metrics", {})
    if r["suite"] == "fixture":
        L += ["", f"Fixture checks passed: {m['passed']}/{m['total']}. By type: "
              + ", ".join(f"{k} {v}" for k, v in m["by_type"].items()) + ".", "",
              "| Item | Type | Result | Failures |", "|---|---|---|---|"]
        for it in r["items"]:
            L.append(f"| {it['id']} | {it['type']} | {'pass' if it['passed'] else '**FAIL**'} | "
                     f"{'; '.join(it['failures'])} |")
        return "\n".join(L) + "\n"
    cols = ("n", "recall@1", "recall@5", "recall@10", "mrr@10", "ndcg@10", "precision@5")
    L += ["", "## Retrieval", "", "| Set | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    rows = [("gating (approved, EN)", m["retrieval"]["gating"])]
    if r.get("v0_baseline"):
        rows.append(("v0 baseline, gating", r["v0_baseline"]["metrics"]["retrieval"]["gating"]))
    rows += [(f"type={k}", v) for k, v in m["retrieval"]["by_type"].items()]
    rows += list(m["retrieval"]["slices"].items())
    for name, s in rows:
        if s.get("n"):
            L.append(f"| {name} | " + " | ".join(str(_fmt(s.get(c))) for c in cols) + " |")
    if r.get("v1_vs_v0"):
        L += ["", "v1 minus v0 (gating set): " + ", ".join(f"{k} {v:+}" for k, v in r["v1_vs_v0"].items()) + "."]
    a = m.get("authority", {})
    L += ["", "## Authority, refusal, latency", "",
          f"- Syllabus-defined questions with an official syllabus passage first: {_fmt(a.get('syllabus_first_rate'))}"
          f" (n={a.get('n')})"]
    if m.get("no_answer"):
        na = m["no_answer"]
        L.append(f"- Unanswerable questions refused (evidence none): {na['correct_refusal_rate']} "
                 f"(n={na['n_unanswerable']}); answerable questions wrongly refused: {na['false_refusal_rate']} "
                 f"(n={na['n_answerable']})")
    lat = m.get("latency", {})
    L.append(f"- Retrieval latency p50 {lat.get('p50_ms')} ms, p95 {lat.get('p95_ms')} ms")
    if m.get("answers"):
        an = m["answers"]
        L += ["", "## Grounded answers", "",
              f"- Accuracy (answered, cites an expected source, covers ≥50% of key terms): {an['accuracy']} "
              f"(n={an['n']})",
              f"- Cites an expected source: {an['cites_relevant_rate']}; mean key-term coverage "
              f"{an['mean_key_coverage']}",
              f"- Fabricated citation markers: {an['fabricated_citations']}; answered with a valid citation: "
              f"{an['citation_valid_rate']}; ungrounded: {an['ungrounded_rate']}",
              f"- Statuses: {an['statuses']}"]
    misses = [i for i in r.get("items", []) if i["type"] != "no_answer" and not i.get("rank")]
    wrong_ref = [i for i in r.get("items", []) if i["type"] == "no_answer" and i.get("refused") is False]
    if misses or wrong_ref:
        L += ["", "## Misses", ""]
        L += [f"- {i['id']} ({i['type']}): expected passage not in the top 10; top was {i.get('top')}" for i in misses]
        L += [f"- {i['id']} (no_answer): not refused, evidence {i.get('evidence')}" for i in wrong_ref]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default=ev.DEFAULT_DATASET)
    ap.add_argument("--thresholds", default=ev.DEFAULT_THRESHOLDS)
    sub = ap.add_subparsers(dest="suite", required=True)
    f = sub.add_parser("fixture")
    f.add_argument("--out", required=True)
    c = sub.add_parser("corpus")
    c.add_argument("--corpus", required=True, help="knowledge_corpus.py output; built there if missing")
    c.add_argument("--pg-dsn", default=os.environ.get("NEXA_KNOWLEDGE_PG_DSN", ""),
                   help="PostgreSQL with pgvector (a scratch schema is created and dropped); empty = in-memory")
    c.add_argument("--out", required=True)
    c.add_argument("--model", default="nexa-embed-default")
    c.add_argument("--v0-model", default="nexa-embed-default")
    c.add_argument("--no-v0", action="store_true", help="skip the v0 baseline")
    c.add_argument("--answers", action="store_true", help="also generate grounded answers (needs the chat model)")
    c.add_argument("--offline", action="store_true", help="stand-in embedders: harness check only")
    c.add_argument("--baseline", default="", help="baseline report (default benchmark/eval/baselines/...)")
    c.add_argument("--write-baseline", action="store_true", help="write this run as the baseline")
    c.add_argument("--record-gate", action="store_true", help="record a passing result for deploy.sh")
    lv = sub.add_parser("live")
    lv.add_argument("--base-url", default="http://127.0.0.1:8103")
    lv.add_argument("--out", required=True)
    lv.add_argument("--no-answers", action="store_true")
    args = ap.parse_args()
    try:
        return {"fixture": run_fixture, "corpus": run_corpus, "live": run_live}[args.suite](args)
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
