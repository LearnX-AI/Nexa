"""NEXA-M2-004 embedding selection rule (v1/knowledge/benchmark/selection.py)."""
import json
import pathlib

from v1.knowledge.benchmark import selection as sel
from v1.knowledge.embeddings import card_for

GOOD_CARD = {"edge_feasible": True, "license": "Apache-2.0", "params_m": 300}


def cand(logical, role, en_hits, tpi_hits, dim=768, card=None, **over):
    """en_hits/tpi_hits: number of questions (of 40/16) answered at rank 1."""
    r = {"logical": logical, "role": role, "dim": dim, "card": card or dict(GOOD_CARD),
         "index_status": "ready", "validation_passed": True, "truncated_pct": 0.0,
         "en_ranks": [1] * en_hits + [None] * (40 - en_hits),
         "tpi_ranks": [1] * tpi_hits + [None] * (16 - tpi_hits)}
    r.update(over)
    return r


def test_score_weights_tok_pisin():
    assert sel.score([1] * 40, [None] * 16) == 60.0
    assert sel.score([None] * 40, [1] * 16) == 40.0


def test_gates():
    g = sel.gates(cand("x", "candidate", 1, 1, truncated_pct=6.0,
                       card={"edge_feasible": False, "license": "Gemma Terms of Use"}))
    assert g == {"G1_served": True, "G2_edge": False, "G3_context": False, "G4_license": False}
    assert not sel.gates(cand("x", "candidate", 1, 1, index_status="failed"))["G1_served"]


def test_incumbent_kept_without_clear_win():
    d = sel.decide([cand("inc", "incumbent", 30, 8), cand("ch", "candidate", 31, 8)])
    assert d["selected"] == "inc" and d["challenger"] == "ch" and "below the switch threshold" in d["reason"]


def test_clear_win_switches():
    d = sel.decide([cand("inc", "incumbent", 20, 2), cand("ch", "candidate", 34, 12)])
    assert d["selected"] == "ch" and d["margin"] >= sel.SWITCH_MARGIN and d["p_better"] >= sel.SWITCH_CONFIDENCE


def test_near_tie_prefers_smaller_vectors_and_ineligible_never_wins():
    d = sel.decide([cand("inc", "incumbent", 10, 0),
                    cand("big", "candidate", 36, 14, dim=1024),
                    cand("small", "candidate", 35, 14, dim=768),
                    cand("control", "control", 40, 16),
                    cand("gemma", "candidate", 40, 16, card={**GOOD_CARD, "license": "Gemma Terms of Use"})])
    assert d["challenger"] == "small" and d["selected"] == "small"


def test_ineligible_incumbent_is_replaced():
    d = sel.decide([cand("inc", "incumbent", 30, 8, truncated_pct=50.0), cand("ch", "candidate", 20, 4)])
    assert d["selected"] == "ch" and d["reason"] == "incumbent not eligible"


def test_candidate_list_is_consistent_with_model_cards():
    root = pathlib.Path(__file__).resolve().parents[1]
    cands = json.loads((root / "knowledge/benchmark/embedding_candidates.json").read_text())["candidates"]
    frag = json.loads((root / "model_gateway/models.embedding-candidates.json").read_text())["models"]
    assert [c["logical"] for c in cands] == [m["logical"] for m in frag]
    assert sum(c["role"] == "incumbent" for c in cands) == 1
    for c in cands:
        assert card_for(c["ollama_tag"]) is not None, c["ollama_tag"]


def test_gold_set_tok_pisin_items_mirror_english():
    root = pathlib.Path(__file__).resolve().parents[1]
    qs = json.loads((root / "knowledge/benchmark/gold_questions.json").read_text())["questions"]
    by = {q["id"]: q for q in qs}
    tpi = [q for q in qs if q["language"] == "tpi"]
    assert len(tpi) == 16
    for q in tpi:
        src = by[q["translation_of"]]
        assert q["files"] == src["files"] and q["evidence"] == src["evidence"] and "review" in q
