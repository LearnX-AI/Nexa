"""NEXA-M2-004 embedding model selection rule, fixed before any results exist.

Inputs are per-candidate evaluation results from v1/tools/knowledge_embed_eval.py.
The rule is deliberately mechanical so the choice can be reviewed and re-run.

1. Gates. A candidate is eligible as the baseline only if:
   G1 served      its index built through the Model Gateway and passed validation
   G2 edge        it can run on a CPU-only school server (model card; section 7)
   G3 context     at most MAX_TRUNCATED_PCT of chunks exceed its input limit
   G4 licence     its licence is in APPROVED_LICENSES (others need a governance decision)
2. Score. S = W_EN * recall@5 (English questions) + W_TPI * recall@5 (Tok Pisin
   questions). Tok Pisin is weighted because PNG learners ask in it and the
   answers are in English curriculum text.
3. Incumbent advantage. Re-embedding the corpus and the school edges has a cost,
   so the incumbent stays unless an eligible challenger beats it by at least
   SWITCH_MARGIN points of S AND a paired bootstrap over questions gives
   P(challenger better) >= SWITCH_CONFIDENCE.
4. Near ties. Among eligible challengers within NEAR_TIE points of the best,
   prefer the smaller vector dimension, then fewer parameters (cheaper storage,
   faster edge queries).
"""
from __future__ import annotations

import random
from typing import Any, Optional

W_EN = 0.6
W_TPI = 0.4
MAX_TRUNCATED_PCT = 5.0
APPROVED_LICENSES = {"Apache-2.0", "MIT"}
SWITCH_MARGIN = 3.0
SWITCH_CONFIDENCE = 0.90
NEAR_TIE = 2.0
BOOTSTRAP_ROUNDS = 2000

RULE_VERSION = "1.0.0"


def recall_at(ranks: list[Optional[int]], k: int) -> float:
    return 100.0 * sum(1 for r in ranks if r is not None and r <= k) / len(ranks) if ranks else 0.0


def score(en_ranks: list[Optional[int]], tpi_ranks: list[Optional[int]]) -> float:
    return round(W_EN * recall_at(en_ranks, 5) + W_TPI * recall_at(tpi_ranks, 5), 2)


def gates(c: dict[str, Any]) -> dict[str, bool]:
    card = c.get("card") or {}
    return {
        "G1_served": bool(c.get("index_status") == "ready" and c.get("validation_passed")),
        "G2_edge": bool(card.get("edge_feasible")),
        "G3_context": c.get("truncated_pct", 100.0) <= MAX_TRUNCATED_PCT,
        "G4_license": card.get("license") in APPROVED_LICENSES,
    }


def p_better(ch: dict[str, Any], inc: dict[str, Any], rounds: int = BOOTSTRAP_ROUNDS, seed: int = 7) -> float:
    """Paired bootstrap over questions: share of resamples where the challenger's
    score exceeds the incumbent's. Resamples within each language."""
    rnd = random.Random(seed)
    en_c, en_i = ch["en_ranks"], inc["en_ranks"]
    tp_c, tp_i = ch["tpi_ranks"], inc["tpi_ranks"]
    wins = 0
    for _ in range(rounds):
        ie = [rnd.randrange(len(en_c)) for _ in en_c] if en_c else []
        it = [rnd.randrange(len(tp_c)) for _ in tp_c] if tp_c else []
        sc = score([en_c[i] for i in ie], [tp_c[i] for i in it])
        si = score([en_i[i] for i in ie], [tp_i[i] for i in it])
        wins += sc > si
    return round(wins / rounds, 3)


def decide(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the rule. Each result needs: logical, role, dim, card, index_status,
    validation_passed, truncated_pct, en_ranks, tpi_ranks."""
    for r in results:
        r["score"] = score(r["en_ranks"], r["tpi_ranks"])
        r["gates"] = gates(r)
        r["eligible"] = all(r["gates"].values()) and r.get("role") in ("incumbent", "candidate")
    incumbent = next((r for r in results if r.get("role") == "incumbent"), None)
    eligible = sorted([r for r in results if r["eligible"] and r is not incumbent],
                      key=lambda r: -r["score"])
    out: dict[str, Any] = {"rule_version": RULE_VERSION, "incumbent": incumbent and incumbent["logical"]}
    if not eligible:
        out.update(selected=incumbent and incumbent["logical"], reason="no eligible challenger")
        return out
    best = eligible[0]["score"]
    near = [r for r in eligible if best - r["score"] <= NEAR_TIE]
    near.sort(key=lambda r: (r["dim"], (r.get("card") or {}).get("params_m", 10**6), -r["score"]))
    challenger = near[0]
    out["challenger"] = challenger["logical"]
    if incumbent is None or not incumbent["eligible"]:
        out.update(selected=challenger["logical"],
                   reason="incumbent not eligible" if incumbent else "no incumbent")
        return out
    margin = round(challenger["score"] - incumbent["score"], 2)
    prob = p_better(challenger, incumbent)
    out.update(margin=margin, p_better=prob)
    if margin >= SWITCH_MARGIN and prob >= SWITCH_CONFIDENCE:
        out.update(selected=challenger["logical"],
                   reason=f"challenger beats incumbent by {margin} points, P(better)={prob}")
    else:
        out.update(selected=incumbent["logical"],
                   reason=f"challenger margin {margin} points, P(better)={prob}; below the switch threshold "
                          f"({SWITCH_MARGIN} points and {SWITCH_CONFIDENCE})")
    return out
