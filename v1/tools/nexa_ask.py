"""Ask the live NEXA v1 services a question from the DGX shell.

    python v1/tools/nexa_ask.py "What is photosynthesis?"              # curriculum search + tutor answer
    python v1/tools/nexa_ask.py "Wanem samting em i photosynthesis?"   # Tok Pisin works too
    python v1/tools/nexa_ask.py --role teacher "..."                   # see what a teacher can see
    python v1/tools/nexa_ask.py --search-only "..."                    # passages and citations only
    python v1/tools/nexa_ask.py --no-context "..."                     # tutor alone, for comparison

1. Knowledge Service (port 8103): finds the top curriculum passages the role
   may see, each with its citation.
2. API Gateway (port 8099) -> agent nexa-tutor: answers using those passages.
   The tutor is not yet wired to the Knowledge Service itself (that is
   M2-005/006), so this script passes the passages in as context.

Needs NEXA_IDENTITY_SECRET in the environment; run from ~/Nexa/Nexa:
    set -a; source <(sudo cat /etc/nexa/identity.env); set +a
"""
import argparse
import os
import sys
import time

import httpx

sys.path.insert(0, os.getcwd())
from v1.identity.tokens import issue_token  # noqa: E402

KNOWLEDGE = "http://127.0.0.1:8103"
API = "http://127.0.0.1:8099"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--role", default="student", help="student, teacher, ai_engineer ...")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--agent", default="nexa-tutor")
    ap.add_argument("--search-only", action="store_true")
    ap.add_argument("--no-context", action="store_true")
    a = ap.parse_args()

    h = {"Authorization": "Bearer " + issue_token(f"user:ask-{a.role}", [a.role], "user", 900)}
    c = httpx.Client(timeout=180)
    passages = []

    if not a.no_context:
        t = time.time()
        r = c.post(f"{KNOWLEDGE}/v1/knowledge/query", headers=h, json={"query": a.question, "top_k": a.top_k})
        if r.status_code != 200:
            print(f"knowledge query failed: {r.status_code} {r.text[:300]}")
            return 1
        body = r.json()
        print(f"\n=== Curriculum passages ({body['x_nexa'].get('returned')} found, "
              f"{int((time.time() - t) * 1000)} ms, index {body['x_nexa'].get('index')})")
        for i, res in enumerate(body["results"], 1):
            md = res.get("metadata") or {}
            cite = res.get("citation") or md.get("title") or md.get("source_uri", "")
            extra = ", ".join(str(md[k]) for k in ("subject", "grade_level", "audience") if md.get(k))
            print(f"\n[{i}] score {res['score']:.3f}  {cite}" + (f"  ({extra})" if extra else ""))
            print("    " + " ".join(res["text"].split())[:400] + ("..." if len(res["text"]) > 400 else ""))
            passages.append(f"[{i}] {cite}\n{res['text']}")
        if a.search_only:
            return 0

    if passages:
        content = ("Answer the student's question using only the curriculum passages below. "
                   "Cite the passages you use as [1], [2] and so on. If they do not contain the "
                   "answer, say so.\n\n" + "\n\n".join(passages) + f"\n\nQuestion: {a.question}")
    else:
        content = a.question
    t = time.time()
    r = c.post(f"{API}/v1/agents/{a.agent}/run", headers=h,
               json={"messages": [{"role": "user", "content": content}]})
    if r.status_code != 200:
        print(f"\nagent call failed: {r.status_code} {r.text[:300]}")
        return 1
    out = r.json()
    print(f"\n=== {a.agent} answer (model {out.get('model')}, {int((time.time() - t) * 1000)} ms)\n")
    print(out["output"])
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
