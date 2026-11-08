"""NEXA-M2-003: normalization, structure recovery, semantic chunking, v0 baseline."""
from __future__ import annotations

import pytest

from v1.knowledge.benchmark.bm25 import BM25
from v1.knowledge.benchmark.v0_baseline import RecursiveSplitter
from v1.knowledge.ingestion.model import Block, BlockKind
from v1.knowledge.processing.chunking import (ChunkingConfig, PositionedBlock, chunk_blocks, count_tokens,
                                              profile_for_subject, split_sentences)
from v1.knowledge.processing.normalize import normalize_block_text, normalize_pages, normalize_unicode
from v1.knowledge.processing.structure import join_cross_page_headings, structure_lines

CFG = ChunkingConfig()


# ---- normalization ----

def test_unicode_keeps_scientific_meaning():
    s = "H₂O, x², 5′→3′, 25 µm, ΔH − 3"
    assert normalize_unicode(s) == s  # NFC, not NFKC
    assert normalize_unicode("eﬃcient ﬁsh") == "efficient fish"
    assert normalize_unicode("soft­hyphen zero​width") == "softhyphen zerowidth"
    assert normalize_unicode("itâ€™s") == "it's"


TOPICS = ["flux", "coils", "cores", "voltage", "current", "turns"]


def _pages(n: int) -> list[str]:
    return [f"{p} | P a g e\nG r a d e  1 2  S T E M  P h y s i c s  S R B\n"
            f"Notes on {TOPICS[p - 1]} in electromagnetic induction for trans-\nformers and self-\nregulating coils.\n"
            f"Question {p}. Explain how {TOPICS[p - 1]} affect induction.\n__________________________\n__________________________\n"
            for p in range(1, n + 1)]


def test_page_normalization_removes_noise_and_keeps_content():
    out, stats = normalize_pages(_pages(6))
    page = out[2]
    assert "P a g e" not in page and "S R B" not in page and "Grade 12 STEM Physics SRB" not in page
    assert "transformers" in page and "self-regulating" in page
    assert page.count("[answer space]") == 1
    c = stats.counts
    assert c["page_number_lines_removed"] == 6 and c["running_header_footer_removed"] == 6
    assert c["letter_spacing_fixed"] == 12 and c["answer_lines_collapsed"] == 6  # headers and page labels


def test_toc_lines_and_block_text():
    out, _ = normalize_pages(["Table of Contents\nRATIONALE ................................ V\nMODULE 12.1 ..... ...... 4"])
    assert "...." not in out[0] and "Table of Contents" in out[0]
    assert normalize_block_text("•  Fill in: ________ now") == "Fill in: ____ now"


# ---- structure recovery ----

def test_structure_recognises_curriculum_and_rejects_table_cells():
    text = ("THEMA HEALTH AND\nMEDICINE\n\nMODULE 12.1: DRUG DEVELOPMENT PROCESS\n\n"
            "Drug development is the process of bringing a new medicine to market once a lead compound "
            "has been identified through drug discovery.\n"
            "Module 12.1\nProtein\nSequencing\n"
            "Activity 1: Exercise\n1. Define the term excipient.\n[answer space]\n2. What is a double blind trial?\n")
    blocks = structure_lines(text)
    heads = [(b.level, b.text) for b in blocks if b.kind == BlockKind.HEADING]
    assert heads[0] == (1, "Thema: Health and Medicine")
    assert heads[1] == (2, "Module 12.1: Drug Development Process")
    assert all("Protein" != t and "Sequencing" != t for _, t in heads)
    ex = [b for b in blocks if b.kind == BlockKind.EXERCISE]
    assert ex and ex[0].text.endswith("[answer space]")


def test_signatures_are_not_headings_and_split_thema_joins():
    blocks = structure_lines("We commend this syllabus to all teachers.\n\nDR UKE KOMBRA, PHD\n"
                             "Secretary for Education\n\nTHEMA\nHEALTH AND MEDICINE\n")
    heads = [b.text for b in blocks if b.kind == BlockKind.HEADING]
    assert heads == ["Thema: Health and Medicine"]


def test_equations_detected_in_science_profile():
    blocks = structure_lines("The voltage ratio is given by:\nVs / Vp = Ns / Np\nwhere N is the number of turns.",
                             aggressive_equations=True)
    assert any(b.kind == BlockKind.EQUATION and "Ns / Np" in b.text for b in blocks)


# ---- chunking ----

def pb(kind, text, page=1, level=0, rows=None, meta=None):
    return PositionedBlock(Block(kind, text, level=level, rows=rows, meta=meta), page, str(page))


LONG = ("Electromagnetic induction produces a current in a coil when the magnetic flux through it changes. "
        "Faraday showed that the induced voltage depends on the rate of change of flux. ")


def _topic(n, title):
    return pb(BlockKind.HEADING, f"Topic {n}: {title}", level=3,
              meta={"curriculum_heading": True, "curriculum": {"topic_num": n, "topic_title": title}})


def _doc():
    module = pb(BlockKind.HEADING, "Module 12.1: Inductive Charging", level=2,
                meta={"curriculum_heading": True, "curriculum": {"module_code": "12.1",
                                                                 "module_title": "Inductive Charging"}})
    return [module, _topic(1, "Electromagnetism")] + [pb(BlockKind.PARAGRAPH, LONG * 2, page=1 + i // 3)
                                                      for i in range(9)] + [
        _topic(2, "Transformers"),
        pb(BlockKind.PARAGRAPH, "Students are expected to: 12.1.2a: Explain how transformers change voltage.", 4),
        pb(BlockKind.PARAGRAPH, "The main parts are listed below:", 4),
        pb(BlockKind.LIST_ITEM, "Primary coil", 4), pb(BlockKind.LIST_ITEM, "Secondary coil", 4),
        pb(BlockKind.LIST_ITEM, "Laminated iron core", 4),
        pb(BlockKind.TABLE, "", 5, rows=[["Type", "Use"], ["Step-up", "Power stations"], ["Step-down", "Chargers"]]),
        pb(BlockKind.EXERCISE, "1. Calculate the secondary voltage. [answer space]", 5),
        pb(BlockKind.LIST_ITEM, "a) 12 V", 5), pb(BlockKind.LIST_ITEM, "b) 24 V", 5),
    ]


def test_chunks_respect_topics_sizes_and_atomic_units():
    chunks = chunk_blocks(_doc(), CFG, "src@abc", context_prefix="Physics | Grade 12")
    assert all(c.tokens <= CFG.max_tokens for c in chunks)
    topics = [c.curriculum.get("topic_title") for c in chunks]
    assert topics.index("Transformers") > 0
    assert all(t == "Electromagnetism" for t in topics[:topics.index("Transformers")])  # no topic mixing
    t2 = [c for c in chunks if c.curriculum.get("topic_title") == "Transformers"]
    joined = "\n".join(c.text for c in t2)
    assert "The main parts are listed below:\n- Primary coil\n- Secondary coil" in joined  # lead-in kept with list
    assert "Type | Use\nStep-up | Power stations\nStep-down | Chargers" in joined           # table whole
    assert "1. Calculate the secondary voltage. [answer space]\n- a) 12 V\n- b) 24 V" in joined  # exercise + options
    assert t2[0].curriculum["outcome_codes"] == ["12.1.2a"] and t2[0].curriculum["module_code"] == "12.1"
    assert chunks[0].embedding_text.startswith("Physics | Grade 12 > Module 12.1: Inductive Charging > Topic 1")


def test_overlap_only_within_a_section_and_neighbours_linked():
    chunks = chunk_blocks(_doc(), CFG, "src@abc")
    t1 = [c for c in chunks if c.curriculum.get("topic_title") == "Electromagnetism"]
    assert len(t1) >= 2 and t1[1].overlap_prefix_chars > 0
    first_t2 = next(c for c in chunks if c.curriculum.get("topic_title") == "Transformers")
    assert first_t2.overlap_prefix_chars == 0
    for a, b in zip(chunks, chunks[1:]):
        assert a.next_chunk_id == b.chunk_id and b.prev_chunk_id == a.chunk_id
    assert t1[0].page_start == 1 and t1[-1].page_end == 3


def test_ids_are_deterministic_and_versioned():
    a = [c.chunk_id for c in chunk_blocks(_doc(), CFG, "src@abc")]
    assert a == [c.chunk_id for c in chunk_blocks(_doc(), CFG, "src@abc")]
    assert set(a).isdisjoint(c.chunk_id for c in chunk_blocks(_doc(), CFG, "src@def"))
    other = ChunkingConfig(target_tokens=200)
    assert other.config_hash() != CFG.config_hash()
    assert set(a).isdisjoint(c.chunk_id for c in chunk_blocks(_doc(), other, "src@abc"))


def test_large_table_split_repeats_header():
    rows = [["Organ", "Function"]] + [[f"Organ {i}", "regulates " + "glucose " * 12] for i in range(60)]
    chunks = chunk_blocks([pb(BlockKind.TABLE, "", rows=rows)], CFG, "t@1")
    assert len(chunks) > 1 and all(c.text.startswith("Organ | Function") for c in chunks)
    assert all(c.tokens <= CFG.max_tokens for c in chunks)


def test_tiny_fragments_fold_and_long_sentences_split():
    blocks = [pb(BlockKind.PARAGRAPH, LONG * 3), pb(BlockKind.PARAGRAPH, "Secretary for Education")]
    chunks = chunk_blocks(blocks, CFG, "x@1")
    assert len(chunks) == 1 and chunks[0].text.endswith("Secretary for Education")
    huge = pb(BlockKind.PARAGRAPH, " ".join(["word"] * 2000))
    assert all(c.tokens <= CFG.max_tokens for c in chunk_blocks([huge], CFG, "x@2"))


def test_config_validation_and_profiles():
    with pytest.raises(Exception):
        ChunkingConfig(target_tokens=10)
    with pytest.raises(Exception):
        ChunkingConfig(unknown=1)
    assert profile_for_subject("Physics") == "physics" and profile_for_subject("English") == "default"
    assert split_sentences("Use e.g. coils. Then measure.") == ["Use e.g. coils.", "Then measure."]
    assert count_tokens("H2O + CO2 -> sugar.") == 7  # "->" is two punctuation pieces


# ---- v0 baseline and BM25 ----

SAMPLE = ("Inductive charging, also known as wireless charging, transfers energy without a plug.\n\n" * 40
          + "Transformers step voltage up or down.\n" * 60 + " ".join(["electromagnetism"] * 400))


def test_v0_splitter_shape():
    chunks = RecursiveSplitter().split_text(SAMPLE)
    assert chunks and all(len(c) <= 1500 for c in chunks)
    assert chunks[1][:40] in chunks[0] or chunks[0][-200:].split()[-1] in chunks[1]  # 300-char overlap


def test_v0_splitter_matches_langchain_when_installed():
    lc = pytest.importorskip("langchain_text_splitters")
    ref = lc.RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=300)
    assert RecursiveSplitter().split_text(SAMPLE) == ref.split_text(SAMPLE)


def test_bm25_ranks_relevant_first():
    bm = BM25(["photosynthesis makes glucose in chloroplasts", "transformers change voltage", "biogas is methane"])
    assert bm.search("how do transformers change voltage", 1)[0][0] == 1


def test_wrapped_thema_titles_join():
    blocks = structure_lines("THEMA: BIOTECHNO\nLOGY\n")
    assert blocks[0].text == "Thema: Biotechnology" and blocks[0].meta["curriculum"]["thema"] == "Biotechnology"
    ai = structure_lines("THEMA:\nARTIFICIAL\nINTELLIGENCE (AI)\n")[0]
    assert ai.text == "Thema: Artificial Intelligence (AI)" and ai.meta["curriculum"]["thema"] == "Artificial Intelligence"
    assert structure_lines("THEMA: BIOTEHCNOLOGY\n")[0].meta["curriculum"]["thema"] == "Biotechnology"
    robotics = [b.text for b in structure_lines("THEMA: ROBOTICS\nINTRODUCTION\nRobots are machines.\n")
                if b.kind == BlockKind.HEADING]
    assert robotics[0] == "Thema: Robotics"
    heads = [b.text for b in structure_lines("THEMA:\nARTIFICIAL\nINTELLIGENCE (AI)\n\nTHEMA: BIOTECHNO-\nLOGY\n\n"
                                             "THEMA: BATTERY\nMODULE 12.1: BIO-BATTERIES\n")
             if b.kind == BlockKind.HEADING]
    assert heads == ["Thema: Artificial Intelligence (AI)", "Thema: Biotechnology", "Thema: Battery",
                     "Module 12.1: Bio-Batteries"]


def test_outcome_code_mismatch_is_flagged():
    blocks = [pb(BlockKind.HEADING, "Module 12.1: Electronics", level=2,
                 meta={"curriculum_heading": True, "curriculum": {"module_code": "12.1"}}),
              _topic(3, "Circuit Boards"),
              pb(BlockKind.PARAGRAPH, "12.2.3a: Define circuit boards. " + LONG)]
    c = chunk_blocks(blocks, CFG, "m@1")[0]
    assert c.curriculum["topic_code"] == "12.1.3" and c.curriculum["code_mismatch"] is True


def test_heading_split_across_pages_is_rejoined():
    pages = join_cross_page_headings(["Body of page one ends here.\nTHEMA: ARTIFICIAL", "INTELLIGENCE\nMachine "
                                      "learning lets computers learn from data.", "Plain page."])
    assert pages[0].endswith("THEMA: ARTIFICIAL INTELLIGENCE") and pages[1].startswith("Machine")
    assert pages[2] == "Plain page."
