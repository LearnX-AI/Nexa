# NEXA-M2-006: Source Provenance, Citations and Grounding Contract

Status: Implemented. Evidence thresholds to calibrate on the DGX (section 4).
Milestone: M2
Aligns with: NEXA-ADR-0001 (Model Gateway), NEXA-ADR-0004, NEXA-M2-001 (locator, authority), NEXA-M2-005 (retrieval)
Implementation: `v1/knowledge/grounding.py`. Endpoints `POST /v1/knowledge/answer`, `GET /v1/knowledge/chunks/{id}`, `GET /v1/knowledge/sources/{version_id}/original`, `GET /v1/knowledge/schema/grounded-answer`.
Tests: `v1/tests/test_knowledge_grounding.py` (10)
Schema: grounded answer `1.0.0` (`CITATION_SCHEMA_VERSION`)

**Status against the acceptance criteria (section 8):** all four are met by tests in CI. The evidence thresholds ship with provisional values. `knowledge_retrieval_bench.py` calibrates them on the gold set with real models.

## 1. Provenance: what every passage carries

Every retrieved passage carries a `provenance` object. It is built from the passage's own stored metadata, which ingestion wrote onto the chunk. So reranking, authority reordering and context construction cannot detach a passage from its origin: they move whole passages and never rewrite their metadata.

| Field | Meaning |
|---|---|
| `source_id` | The document, stable across versions, e.g. `png-ndoe:stem-chemistry-syllabus-gr11` or `lms:edunex-ncd:cm345` |
| `version_id`, `content_checksum` | The exact version: `source_id@<16 hex>` and the SHA-256 of the original bytes |
| `chunk_id`, `run_id`, `chunker_version` | The passage, and the processing run and chunker that produced it |
| `page_start`, `page_end`, `page_label`, `page_basis` | Location. `page_basis` is `physical` for PDF, `rendered_estimate` for DOCX, `slide` for PPTX, or `none` for HTML. `page_label` is the printed label when it differs from the page number. |
| `section_path`, `anchor` | Heading path, and a stable in-document anchor (for example a DOCX paragraph index) |
| `citation` | The human-readable reference, e.g. "Chemistry Syllabus Grade 11, p. 2" |
| `title`, `source_type`, `authority_tier`, `scope`, `school_id`, `language` | What kind of source it is (M2-001) |
| `source_uri`, `lms` | Where the original lives. For LMS content: system, site, course, item, revision and URL. |
| `index_id`, `embedding` | The index version and embedding fingerprint that found it (M2-004) |

## 2. Citation payload: schema 1.0.0

`POST /v1/knowledge/answer` takes a `question`, plus optional `top_k`, `filters`, `audience` and `explain`. It returns a `GroundedAnswer`; its JSON Schema is at `GET /v1/knowledge/schema/grounded-answer`.

```json
{
  "schema_version": "1.0.0",
  "status": "grounded",
  "answer": "Ionic bonds form when electrons transfer from a metal atom to a non-metal atom [S1].",
  "confidence": "high",
  "notes": [],
  "citations": [{
    "marker": "S1", "rank": 1, "used": true, "authoritative": true,
    "quote": "Ionic Bonding Ionic bonds form when electrons transfer ...",
    "support": 0.83,
    "provenance": {"source_id": "png-ndoe:chem-syllabus-gr11", "version_id": "png-ndoe:chem-syllabus-gr11@3f2a...",
                   "content_checksum": "sha256:3f2a...", "chunk_id": "...", "page_start": 2, "page_basis": "physical",
                   "citation": "Chemistry Syllabus Grade 11, p. 2", "index_id": "idx_...", "embedding": "..."}
  }],
  "evidence": {"level": "strong", "conflict": false, "top_similarity": 0.71, "top_coverage": 0.8,
               "passages": 5, "thresholds": {...}, "reasons": ["top similarity 0.710, word coverage 80%"]},
  "integrity": {"valid_markers": ["S1"], "fabricated_markers": [], "uncited_sentences": 0,
                "weakly_supported_sentences": 0},
  "model": "nexa-chat-precise"
}
```

The fields:
- **`status`** is one of:
  - `grounded`
  - `grounded_weak`
  - `no_evidence`
  - `ungrounded`
  - `generation_unavailable`
- **`citations`** lists every passage given to the model, in rank order. `used: true` marks the passages the answer actually cites.
- **`quote`** is the verbatim opening of the passage.
- **`support`** is the word overlap between the sentences citing a passage and that passage.

## 3. Behaviour by evidence

Evidence is graded before any model call, from the top passage's similarity and its word coverage of the question (M2-005 `coverage`).

| Situation | Behaviour |
|---|---|
| **No evidence**: no passage after filters, or similarity below `min` and word coverage below 60% | **No model call.** Status `no_evidence`, `confidence: none`, and a fixed message: *"I could not find this in the approved curriculum materials available to you, so I will not guess..."* NEXA does not answer from general knowledge here. |
| **Weak evidence**: between `min` and `strong` | The model answers only from the passages. Status `grounded_weak`, `confidence: low`, and the fixed note *"The approved material only partly covers this question..."* |
| **Conflicting sources**: M2-005 arbitration moved an official passage above a lower-authority one on the same point | The context labels each source authoritative or supplementary, and the model is told supplementary sources must not contradict authoritative ones. The answer carries a note that it follows the official curriculum, and `evidence.conflict` is true. |
| **The model finds the passages do not answer** | The model replies `INSUFFICIENT_EVIDENCE`. Status `no_evidence`, and the passages are still listed. |
| **The model cites nothing valid** | The answer is **withheld**. Status `ungrounded`, a fixed message, and the passages are listed so the learner can read them. |
| **Gateway down** | Status `generation_unavailable`, and the passages are listed. |
| **Strong evidence** | Status `grounded`, `confidence: high`, or `low` if any cited sentence is weakly supported. |

## 4. Evidence thresholds

Cosine similarity scales differ between embedding models, so thresholds are kept per prefix scheme in `config.EVIDENCE_THRESHOLDS`. They can be overridden with `NEXA_EVIDENCE_MIN` and `NEXA_EVIDENCE_STRONG`.

**Provisional values:**

| Model | `min` | `strong` |
|---|---|---|
| nomic | 0.50 | 0.62 |
| bge-m3 | 0.45 | 0.58 |

Word coverage: strong at 0.60 and above, minimum 0.34.

**Calibration.** `knowledge_retrieval_bench.py` records the top passage's similarity and word coverage for two groups of questions:
- **answerable:** the gold-set questions whose answer is found in the top 5;
- **unanswerable:** 16 questions the corpus does not answer (`benchmark/unanswerable_questions.json`), a mix of off-curriculum questions and on-subject questions it does not cover.

It then suggests thresholds:
- **`min`** is the cut that best separates unanswerable questions from answerable ones.
- **`strong`** is the 25th percentile of answerable similarities.

**DGX calibration, to do:** fill in the suggested values for the active model and the separation accuracy.

## 5. Citation integrity

The model sees each passage as one numbered block, `[S1] <citation> (<source_type>, authoritative|supplementary)`, followed by the passage text. Context construction cuts passages to fit the budget but never merges them, so each marker stays one chunk with one provenance.

After generation (`grounding.validate`):
1. **Markers are checked against the given passages.** A marker that does not refer to a given passage, like `[S9]` when there are five, is removed and listed in `fabricated_markers`. So is any numbered reference such as `[3]`. Groups like `[S1, S2]` are normalised to `[S1][S2]`.
2. **No valid citation, no answer.** An answer left with no valid citation is withheld (`ungrounded`).
3. **Weakly supported sentences are reported.** A cited sentence sharing less than `NEXA_CITATION_MIN_SUPPORT` (25%) of its words with the passages it cites is counted as weakly supported, and lowers `confidence`. It is reported rather than removed, because paraphrase is legitimate.
4. **Uncited sentences are counted.** Content sentences without a citation are counted in `uncited_sentences`.

## 6. Traceability

A citation resolves back to the original document location:

1. `GET /v1/knowledge/chunks/{chunk_id}` returns:
   - the chunk text and its provenance;
   - its source version (status, title, checksum, file name, MIME type, page count, approver);
   - a path to the original.

   It returns 404 unless the caller could have retrieved that chunk, so it never reveals teacher-only material to a student.
2. `GET /v1/knowledge/sources/{version_id}/original` returns the original file byte for byte, with its checksum in `X-NEXA-Checksum`. It is for content staff (`knowledge.ingest`).
3. The page, printed label, section path and anchor locate the passage in that file.

## 7. Tests

- **`test_end_to_end_traceability_across_formats_and_subjects`** covers four formats and four subjects:
  - Chemistry: PDF, two pages, with the answer on page 2;
  - Biology: DOCX teacher guide;
  - Physics: PPTX lesson notes;
  - Mathematics: LMS HTML.

  For each one, the chain is: answer → used citation → `/chunks/{id}` → source version → `/original` bytes, whose SHA-256 equals the citation's `content_checksum`. The chunk text contains the expected passage, and the quote is its verbatim opening. The PDF citation points at page 2, and the LMS citation carries the item ID and revision.
- **`test_provenance_survives_rerank_and_context`**: after reranking, every result's provenance matches the registry. In the model's context, every marker is followed by exactly that chunk's text.
- **`test_fabricated_citations_are_removed_and_reported`** covers three cases:
  - an answer with invented markers: they are removed and reported;
  - an answer with only invented markers: withheld;
  - an answer with no markers: withheld.
- **`test_no_evidence_is_a_controlled_fallback_without_a_model_call`** covers three ways to have no evidence:
  - an off-curriculum question;
  - filters that leave nothing;
  - the model replying `INSUFFICIENT_EVIDENCE`.
- **Other fallbacks:** `test_weak_evidence_is_marked_low_confidence`, `test_conflicting_sources_follow_the_official_one`, `test_generation_failure_returns_passages`.
- **Access and schema:** `test_chunk_trace_respects_access`, `test_citation_schema_is_published`, `test_validate_marker_forms_and_support`.

## 8. Acceptance criteria (NEXA-M2-006)

| Criterion | Status | Evidence |
|---|---|---|
| Citation payload schema documented | **Met** | Section 2; `GET /v1/knowledge/schema/grounded-answer` (JSON Schema 1.0.0) |
| End-to-end tests show source traceability from user answer back to original document location | **Met** | Sections 6 and 7; four formats, four subjects, checksum-verified original |
| Missing/weak evidence triggers controlled fallback rather than unsupported certainty | **Met.** Thresholds are provisional until the DGX calibration (section 4). | Section 3; no-evidence, weak, ungrounded and generation-failure tests |
| Citation integrity tests pass across representative subjects and formats | **Met** | Section 5; fabricated-marker, support and context-integrity tests across Chemistry, Biology, Physics and Mathematics in PDF, DOCX, PPTX and HTML |

## 9. Operations and limitations

Configuration:
- **Answer model:** `NEXA_ANSWER_MODEL`, default `nexa-chat-precise`, a logical model in the gateway registry. It is called with the Knowledge Service's own gateway token.
- **Context budget:** `NEXA_ANSWER_CONTEXT_CHARS`, default 7,000.

Limitations:
- **Support is lexical.** The support score is word overlap, a cheap first check. A model-based check (natural language inference) would catch unsupported claims phrased with the source's words. It belongs with M2-008's evaluation.
- **The tutor is not wired in yet.** The `nexa-tutor` agent does not call `/answer` yet; wiring agents to the grounded answer is M3 agent work. Until then, `v1/tools/nexa_ask.py` and the API can be used directly.
