"""Classroom load test for a NEXA answer service (NEXA-M2-009).

Sends N students' questions to /v1/knowledge/answer at once (or spread over a
window) and reports how long students waited, how many got an answer, and
whether the answers stayed correct under load.

    set -a; . /etc/nexa-edge/edge.env; set +a
    python v1/tools/nexa_load.py --students 10 --out ~/nexa-evidence
    python v1/tools/nexa_load.py --students 30 --spread 60 --out ~/nexa-evidence
    python v1/tools/nexa_load.py --students 100 --repeat-share 0.3 --out ~/nexa-evidence

For numbers that mean something:
- questions are real curriculum questions from the M2-008 dataset (answerable,
  unanswerable and misspelt), with a known expected source and key terms, so
  every answer is scored, not only timed;
- each student's question text is distinct unless --repeat-share asks for some
  students to repeat a popular question; distinct text cannot be served from
  the answer cache, so the run measures real generation;
- one warm-up question (not counted) loads the models first;
- latency is measured with a monotonic clock from just before the request is
  sent until the full answer has arrived;
- the service's own counters (answer queue, cache, routes) are read before and
  after; restart the service between runs so each run starts clean.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import math
import os
import random
import statistics
import sys
import time
from typing import Any, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from v1.knowledge.benchmark import evalsuite as ev  # noqa: E402

# Wordings that keep the meaning but give distinct text (and so no cache hit).
VARIANTS = ("{q}", "Can you tell me: {q}", "Please explain: {q}", "I want to know: {q}", "Question: {q}",
            "{q} Please answer clearly.", "Help me understand: {q}", "Teacher asked us: {q}")


def pct(xs: list[float], p: float) -> Optional[float]:
    xs = sorted(xs)
    if not xs:
        return None
    return round(xs[min(len(xs) - 1, max(0, math.ceil(p * len(xs)) - 1))], 2)


def plan(ds: dict[str, Any], n: int, repeat_share: float, seed: int) -> list[dict[str, Any]]:
    rnd = random.Random(seed)
    pool = [i for i in ds["items"] if i["suite"] == "corpus" and i.get("approved", True)
            and i["type"] in ("exact_fact", "conceptual", "ambiguous", "no_answer", "spelling")]
    rnd.shuffle(pool)
    popular = pool[0]
    out, used = [], set()
    for k in range(n):
        if k > 0 and rnd.random() < repeat_share:
            it, text = popular, popular["question"]
        else:
            it = pool[k % len(pool)]
            v = VARIANTS[(k // len(pool)) % len(VARIANTS)]
            text = v.format(q=it["question"])
            while text in used:
                text = text + " "
        used.add(text)
        out.append({"student": k + 1, "item": it, "question": text,
                    "repeat": it is popular and k > 0 and text == popular["question"]})
    return out


def score(item: dict[str, Any], body: dict[str, Any]) -> Optional[bool]:
    status = body.get("status")
    if item["type"] == "no_answer":
        return status not in ("grounded", "grounded_weak")   # a refusal or a general answer, never curriculum
    if status in ("generation_unavailable", None):
        return None
    return ev.answer_correct(body, item)["correct"]


async def run(args) -> dict[str, Any]:
    import httpx

    from v1.identity import tokens
    ds = ev.load_dataset(args.dataset)
    students = plan(ds, args.students, args.repeat_share, args.seed)
    rnd = random.Random(args.seed + 1)
    starts = [0.0] * len(students) if args.spread <= 0 else sorted(rnd.uniform(0, args.spread) for _ in students)
    tok = tokens.issue_token("load:student", [args.role], "user", 7200)
    h = {"Authorization": f"Bearer {tok}"}
    base = args.base_url.rstrip("/")
    limits = httpx.Limits(max_connections=args.students + 5, max_keepalive_connections=args.students + 5)
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout), limits=limits) as c:
        health = (await c.get(base + "/health")).json()
        before = (await c.get(base + "/v1/edge/status", headers=h)).json() if args.edge_status else None
        if args.warmup:
            t = time.monotonic()
            await c.post(base + "/v1/knowledge/answer", headers=h,
                         json={"question": "Warm-up: what is photosynthesis in plants?"})
            warm = round(time.monotonic() - t, 1)
        else:
            warm = None
        t0 = time.monotonic()

        async def one(s: dict[str, Any], delay: float) -> dict[str, Any]:
            await asyncio.sleep(delay)
            sent = time.monotonic()
            rec = {"student": s["student"], "id": s["item"]["id"], "type": s["item"]["type"],
                   "repeat": s["repeat"], "sent_s": round(sent - t0, 3)}
            try:
                r = await c.post(base + "/v1/knowledge/answer", headers=h, json={"question": s["question"]})
                rec["latency_s"] = round(time.monotonic() - sent, 3)
                rec["http"] = r.status_code
                if r.status_code == 200:
                    b = r.json()
                    rec.update(status=b.get("status"), route=(b.get("x_nexa") or {}).get("route"),
                               correct=score(s["item"], b))
                else:
                    rec["error"] = r.text[:200]
            except httpx.HTTPError as exc:
                rec.update(latency_s=round(time.monotonic() - sent, 3), http=None, error=type(exc).__name__)
            return rec
        results = await asyncio.gather(*(one(s, d) for s, d in zip(students, starts)))
        makespan = time.monotonic() - t0
        after = (await c.get(base + "/v1/edge/status", headers=h)).json() if args.edge_status else None
    return summarise(args, results, makespan, health, before, after, warm)


def summarise(args, results, makespan, health, before, after, warm) -> dict[str, Any]:
    n = len(results)
    ok = [r for r in results if r.get("http") == 200]
    answered = [r for r in ok if r.get("status") not in ("generation_unavailable", None)]
    lat_all = [r["latency_s"] for r in results if "latency_s" in r]
    lat_ans = [r["latency_s"] for r in answered]
    scored = [r for r in answered if r.get("correct") is not None]
    by = lambda key: dict(sorted(((k, sum(1 for r in results if r.get(key) == k))  # noqa: E731
                                  for k in {r.get(key) for r in results}), key=lambda kv: str(kv[0])))
    m = {
        "students": n, "arrival": "burst" if args.spread <= 0 else f"spread over {args.spread:.0f}s",
        "repeat_share": args.repeat_share,
        "got_an_answer": len(answered), "got_an_answer_rate": round(len(answered) / n, 3) if n else 0,
        "generation_unavailable": sum(1 for r in ok if r.get("status") == "generation_unavailable"),
        "http_errors": sum(1 for r in results if r.get("http") not in (200,)),
        "latency_s": {"p50": pct(lat_all, .5), "p90": pct(lat_all, .9), "p95": pct(lat_all, .95),
                      "p99": pct(lat_all, .99), "max": round(max(lat_all), 2) if lat_all else None,
                      "mean": round(statistics.mean(lat_all), 2) if lat_all else None},
        "latency_answered_s": {"p50": pct(lat_ans, .5), "p95": pct(lat_ans, .95)},
        "makespan_s": round(makespan, 1),
        "answers_per_minute": round(len(answered) / makespan * 60, 1) if makespan else None,
        "correct_rate": round(sum(1 for r in scored if r["correct"]) / len(scored), 3) if scored else None,
        "scored": len(scored), "routes": by("route"), "statuses": by("status"), "warmup_s": warm,
    }
    return {"tool": "nexa_load", "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "base_url": args.base_url, "label": args.label, "service": {
                "edge": health.get("edge"), "active_index": health.get("active_index"),
                "generation_before": (before or {}).get("generation"),
                "generation_after": (after or {}).get("generation"), "cache_after": (after or {}).get("cache")},
            "metrics": m, "requests": sorted(results, key=lambda r: r["student"])}


def markdown(rep: dict[str, Any]) -> str:
    m = rep["metrics"]
    g = rep["service"].get("generation_after") or {}
    cr = "-" if m["correct_rate"] is None else f"{m['correct_rate']:.0%}"
    L = [f"# NEXA classroom load test{': ' + rep['label'] if rep['label'] else ''}", "",
         f"{rep['generated_at']}, {rep['base_url']}, {m['students']} students, {m['arrival']}, "
         f"repeat share {m['repeat_share']}. Service: {json.dumps(rep['service'].get('edge'))}.", "",
         "| Measure | Value |", "|---|---|",
         f"| Students who got an answer | {m['got_an_answer']}/{m['students']} ({m['got_an_answer_rate']:.0%}) |",
         f"| 'Answer unavailable' (queue or model timeout) | {m['generation_unavailable']} |",
         f"| HTTP errors or client timeouts | {m['http_errors']} |",
         f"| Wait, median / p90 / p95 / max | {m['latency_s']['p50']} / {m['latency_s']['p90']} / "
         f"{m['latency_s']['p95']} / {m['latency_s']['max']} s |",
         f"| Whole class served in | {m['makespan_s']} s ({m['answers_per_minute']} answers/min) |",
         f"| Answers correct (expected source + key terms; refusal for off-curriculum) | {cr} of {m['scored']} scored |",
         f"| Routes | {m['routes']} |", f"| Statuses | {m['statuses']} |",
         f"| Answer queue (service) | concurrency {g.get('concurrency')}, max waiting {g.get('max_waiting')}, "
         f"mean wait {g.get('mean_wait_s')} s, max wait {g.get('max_wait_s')} s, queue timeouts {g.get('timed_out')}, "
         f"model-outage retries {g.get('outage_retries', 0)} |",
         f"| Warm-up (not counted) | {m['warmup_s']} s |", ""]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8103")
    ap.add_argument("--students", type=int, required=True)
    ap.add_argument("--spread", type=float, default=0.0, help="seconds over which arrivals are spread; 0 = all at once")
    ap.add_argument("--repeat-share", type=float, default=0.0, help="share of students asking the same popular question")
    ap.add_argument("--role", default="student")
    ap.add_argument("--timeout", type=float, default=1800.0, help="client timeout per request, seconds")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-warmup", dest="warmup", action="store_false")
    ap.add_argument("--no-edge-status", dest="edge_status", action="store_false")
    ap.add_argument("--dataset", default=ev.DEFAULT_DATASET)
    ap.add_argument("--label", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if not os.environ.get("NEXA_IDENTITY_SECRET"):
        print("load the service settings first: set -a; . /etc/nexa-edge/edge.env; set +a", file=sys.stderr)
        return 2
    rep = asyncio.run(run(args))
    os.makedirs(args.out, exist_ok=True)
    stem = f"nexa-load-{args.students}s-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    with open(os.path.join(args.out, stem + ".json"), "w") as fh:
        json.dump(rep, fh, indent=2)
    md = markdown(rep)
    with open(os.path.join(args.out, stem + ".md"), "w") as fh:
        fh.write(md)
    print(md)
    print(f"report: {os.path.join(args.out, stem)}.md / .json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
