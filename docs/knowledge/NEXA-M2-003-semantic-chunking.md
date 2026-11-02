# NEXA-M2-003: Semantic Chunking and Content Normalization

Status: Draft for governance acceptance
Milestone: M2
Versions: normalizer 1.0.0, chunker `nexa-structural` 1.0.0
Aligns with: NEXA-ADR-0004, NEXA-MIG-001
Implementation: `v1/knowledge/processing/` (normalize, structure, chunking)
Evidence: `docs/knowledge/evidence/m2-003-benchmark-report.json`,
`docs/knowledge/evidence/m2-003-chunk-review-sample.jsonl`

## 1. Normalization (normalizer 1.0.0)

The goal is to remove extraction noise without changing educational meaning.

Deliberately not done:
- **No NFKC.** NFKC folds H₂O to H2O, x² to x2, µ to μ and the prime in 5′→3′ to
  an apostrophe. NFC plus an explicit, reviewed replacement map is used instead.
- No spelling correction, case changes, or removal of numbers, symbols or units.

Done, in order, with every change counted on the run (`quality.normalization`):

| Step | Example from the corpus |
|---|---|
| Ligatures, invisible characters, Symbol-font bullets, common mojibake | `ﬁ` → `fi`, soft hyphens removed, `â€™` → `'` |
| Letter-spaced lines repaired | `G r a d e  1 2  S T E M  P h y s i c s  S R B` |
| Repeated running headers and footers removed, document-wide | `STEM Grade 12 Chemistry Syllabus – Term 1` on every page |
| Printed page-number lines removed; the label is kept as provenance | `VIII \| P a g e` → page label `VIII` |
| Table-of-contents dot leaders removed | `RATIONALE ........ V` |
| Worksheet answer lines collapsed to one marker | `________` × 6 → `[answer space]` |
| OCR noise lines dropped (OCR pages only) | stray `\|`, `~`, single glyphs |
| De-hyphenation of words broken across lines, keeping real compounds | `trans-` / `formers` → `transformers`; `self-` / `regulating` → `self-regulating` |

Header and footer candidates must be 80 characters or fewer, must not end
mid-word or mid-list, and are never answer lines, so body text that happens to
repeat is not removed.

## 2. Structure recovery

PDF and OCR text arrives as wrapped lines. The structurer rebuilds headings,
paragraphs, list items, exercises (with their answer space and options) and
equations. DOCX, PPTX and HTML already carry structure and skip this step.

- PNG curriculum headings are recognised exactly: upper-case `THEMA` and
  `MODULE n.n:` with upper-case titles. Mixed-case `Thema` and `Module 12.1`
  lines are table cells and cross-references and are not headings.
- Headings wrapped over several lines or across a page break are rejoined
  (`THEMA:` / `ARTIFICIAL` / `INTELLIGENCE (AI)`).
- Generic headings must start after a finished sentence or a blank line, must
  not run on into a lower-case line or end on a conjunction, and a lone
  title-case word is not a heading. This keeps wrapped syllabus table cells
  ("Function", "Purification", "12.1.1b: Draw and") out of the heading tree.
- Signature lines (`DR UKE KOMBRA, PHD`) are not headings.

## 3. Chunking (`nexa-structural` 1.0.0)

1. Blocks are walked with a heading stack, so every block knows its section
   path and curriculum position: thema, module code and title, topic code and
   title.
2. Blocks are grouped into **atomic units** that are never cut:
   - a table (split by rows with the header repeated only if it alone exceeds
     the limit),
   - an exercise with its answer space and its answer options,
   - a worked example,
   - an equation with the sentence that introduces it,
   - a lead-in sentence ending in `:` with the list it introduces.
3. Units are packed up to `target_tokens` within a section. THEMA, MODULE and
   Topic headings, and document headings at or above `hard_boundary_level`,
   always start a new chunk, so **a chunk never mixes two topics**. Smaller
   subsections pack together with their titles kept inline.
4. Oversized paragraphs split on sentence boundaries; a single sentence over
   `max_tokens` splits on word boundaries.
5. **Overlap** (about `overlap_tokens` of whole trailing sentences) is added
   only when one section continues into the next chunk. There is no overlap
   across topics: the context header carries continuity instead.
6. Fragments under half of `min_tokens` (cover lines, sign-offs) fold into a
   neighbour at the same curriculum position and top-level section.
7. If a chunk's outcome codes disagree with its heading numbering (a source
   error), both are kept and the chunk is flagged `code_mismatch`.

### 3.1 Configuration (versioned)

| Field | Default | |
|---|---|---|
| `target_tokens` | 320 | Packing target |
| `max_tokens` | 512 | Hard limit for non-atomic content |
| `min_tokens` | 60 | Below this a chunk merges with a neighbour |
| `overlap_tokens` | 48 | Sentence-aligned, within a section only |
| `hard_boundary_level` | 2 | Document headings at or above this level always cut |
| `atomic_overflow` | 1.6 | An atomic unit may exceed `max_tokens` by this factor before it is split |
| `include_context_header` | true | Embedding text starts with subject, grade, term, title and section path |
| `subject_profile` | from subject | `physics`, `chemistry` and `mathematics` detect equations more aggressively and allow worked examples up to 2× `max_tokens`; `biology` and `default` use the base rules |

The config is a frozen model; its SHA-256 hash is part of every chunk id, so a
config change produces a new, separate chunk set (see M2-002 section 5). Any
upload can override it through `manifest.chunking`.

### 3.2 Chunk record

Every chunk carries complete provenance:

| Group | Fields |
|---|---|
| Identity | `chunk_id`, `ordinal`, `prev_chunk_id`, `next_chunk_id`, `section_id` |
| Content | `text` (for display and citation), `embedding_text` (context header + text), `tokens`, `overlap_prefix_chars`, `block_kinds` |
| Location | `locator`: `page_start`, `page_end`, `page_label`, `page_basis`, `section_path`, `anchor`; `anchors` |
| Curriculum | `thema`, `module_code`, `module_title`, `topic_code`, `topic_title`, `outcome_codes`, `code_mismatch` |
| Source | The flat M2-001 record: `source_id`, `version_id`, checksum, type, tier, subject, grades, term, scope, approval, access, effective dates, `source_uri` |
| Extraction | `run_id`, methods (native, ocr), `min_ocr_confidence`, `low_confidence`, extractor and OCR engine versions |
| Chunking | strategy, chunker, normalizer and pipeline versions, `config_hash`, full config |
| Citation | Rendered, e.g. `STEM Physics Syllabus, Grade 12, Term 2, p. 22 [printed 19], Thema: Battery Technology > Module 12.1: Inductive Charging` |

## 4. Chunk quality review

Eighteen chunks were reviewed in full on the real corpus, spanning syllabus
outcome pages, teacher guide activities, student guide exercises, multi-chunk
physics prose with overlap, lesson-note tables, OCR pages, PPTX slides, and a
flagged code mismatch. The sample, with complete records, is
`docs/knowledge/evidence/m2-003-chunk-review-sample.jsonl`.

What held up:
- Outcome codes attach to the right topic, and syllabus chunks read as one
  topic's performance indicators, content and skills.
- Lesson-note tables survive whole with their header row and stay with their
  section text.
- Exercises keep their questions and options together.
- Overlap appears only inside a continuing section, and starts on a sentence.
- Citations carry the printed page where the PDF prints one.
- OCR chunks from good scans read cleanly (94.7 confidence). The degraded page
  is flagged and visibly worse, as intended.
- PPTX chunks keep the slide title, bullets and speaker notes together.

The review found five issues, and four were fixed before sign-off:

| Finding | Fix |
|---|---|
| DOCX text boxes appeared twice ("Figure 3: …Figure 3: …"): Word stores a DrawingML copy and a legacy VML fallback | Extractor skips `mc:Fallback`; 0 doubled lines in the corpus afterwards |
| Thema titles truncated (`Artificial`, `Biotechno`, `Health And`) by headings wrapped over lines and pages | Iterative rejoin across lines and page breaks; thema values canonicalised. The corpus now has exactly 7 thema values: Artificial Intelligence, Battery Technology, Biodiversity, Biotechnology, Energy, Health and Medicine, Robotics |
| Front-matter fragments ("Secretary for Education") isolated by a signature line read as a heading | Signature lines are not headings; tiny fragments fold within their section. Chunks under 30 tokens fell from 4.0% (75) to 1.2% |
| Heading numbering and outcome codes disagree in the source (Module 12.1 with `12.2.3x` outcomes) | Not a pipeline error. Both kept, chunk flagged `code_mismatch` (9 chunks, 4 documents) for the content team |
| PDF tables are flattened to text, so syllabus overview tables become long run-on lines | Known limitation: pypdf exposes no table structure. Words are retained (99.6% PDF retention). Revisit with a layout-aware PDF parser if retrieval on those tables proves weak |

## 5. Benchmark: v0 against v1

### 5.1 Method

- **Corpus:** all 72 files (29 native PDFs, 41 DOCX, 1 scanned PDF, 1 PPTX).
- **v0:** the exact v0 loaders and splitter: PyPDFLoader per page, DOCX
  paragraphs only, `RecursiveCharacterTextSplitter(1500, 300)`. The splitter is
  a dependency-free port verified identical to LangChain's on 453 real texts
  (864 chunks, 0 differences); a test re-checks it whenever LangChain is
  installed.
- **v1:** this standard, default config.
- **Retriever held fixed:** BM25, so only the chunking differs. BM25 runs
  anywhere, including CI. The same gold set should be re-run with dense
  embeddings through the Model Gateway on the DGX before the retrieval step is
  designed (section 6).
- **Gold set:** 40 questions written in learner and teacher wording, not copied
  from the source (`v1/knowledge/benchmark/gold_questions.json`): 15 PDF prose,
  5 syllabus outcomes, 4 DOCX prose, 14 DOCX tables, 1 PPTX, 1 OCR. A hit is a
  chunk from an expected file containing every evidence phrase. All 40 were
  verified answerable from the corpus.
- **Reproduce:** `python v1/tools/knowledge_corpus.py --out DIR`, then
  `python v1/tools/knowledge_benchmark.py --corpus DIR`.

### 5.2 Structure, noise, retention, provenance

| Metric | v0 | v1 |
|---|---|---|
| Files producing chunks | 70 of 72 | **72 of 72** |
| Chunks | 2,310 | 1,811 |
| Tokens p5 / median / p95 / max | 43 / 236 / 334 / 1,183 | 65 / 248 / 344 / 834 |
| Chunks under 30 tokens | 4.2% | **1.2%** |
| Chunks starting mid-sentence | 20.6% | **0.4%** |
| Chunks carrying header, page-label, TOC or answer-line noise | 43.6% | **0.7%** |
| Content retention, all formats | 99.1% | **99.7%** |
| Retention: DOCX / PDF / scanned PDF / PPTX | 97.3 / 99.9 / 0.0 / 0.0 | 99.9 / 99.6 / 98.3 / 100.0 |
| DOCX table rows kept intact in one chunk (140 rows) | 0.7% | **100%** |

Provenance on every chunk:

| Field | v0 | v1 |
|---|---|---|
| File, subject, grade, document type | 100% | 100% |
| Term or module | 86.7% | 99.4% |
| Physical page | 86.7% | 100% |
| Printed page label | none | 68.6% |
| Section path | none | 88.3% |
| Module / topic / outcome codes | none | 45.3% / 39.8% / 13.5% |
| Version, authority tier, access class, approval | none | 100% each |
| Chunker version, citation, neighbour links | none | 100% each |

Module, topic and outcome coverage track the documents: syllabi and guides
carry them on most chunks; front matter and lesson notes have none to carry.

### 5.3 Retrieval (BM25, 40 questions)

| | Recall@1 | Recall@3 | Recall@5 | MRR@10 |
|---|---|---|---|---|
| v0 | 37.5% | 52.5% | 62.5% | 0.470 |
| **v1** | **55.0%** | **72.5%** | **82.5%** | **0.670** |
| v1 without context header | 50.0% | 62.5% | 75.0% | 0.604 |
| v1 with M2-001 authority weights on every query | 52.5% | 67.5% | 80.0% | 0.632 |

Recall@5 by category:

| Category (n) | v0 | v1 |
|---|---|---|
| PDF prose (15) | 93.3% | 93.3% |
| Syllabus outcomes (5) | 100% | 80.0% |
| DOCX prose (4) | 75.0% | 75.0% |
| DOCX tables (14) | 7.1% | **71.4%** |
| PPTX (1) | 100%* | 100% |
| OCR (1) | 100%* | 100% |

\* v0 answers these only from the DOCX and native-PDF copies of the same
content. It cannot read the PPTX or the scanned PDF at all.

### 5.4 Reading the results

- **The largest gain is content v0 never indexed.** v0 dropped every DOCX
  table, so lesson-note questions answered by a table were near-unanswerable
  (7%). v1 answers 71%.
- **v1 removes almost all noise.** 43.6% of v0 chunks carried page labels,
  running headers, TOC leaders or answer lines; 20.6% started mid-sentence.
- **The context header helps** (82.5% against 75.0% recall@5 without it).
  One measured cost: the header repeats "Genetics and Molecular Biology" on
  every lesson-note chunk, so a generic question like "What is molecular
  biology the study of?" (q24) loses discrimination under lexical retrieval.
- **One regression: syllabus outcomes, 100% to 80%.** The teacher guides
  restate the syllabus outcomes word for word, and better-formed teacher-guide
  chunks now compete with the syllabus chunks the gold set requires. Applying
  the M2-001 tier weights restores outcome questions to 100%, but applied to
  every query it costs lesson-note tables (71% to 64%). That is the evidence for
  authority rule R3: retrieval should weight by claim class (outcome questions
  favour the syllabus; explanatory questions do not), not uniformly.

## 6. Limitations and next steps

- BM25 measures lexical retrieval. Re-run the gold set with
  `nexa-embed-default` through the Model Gateway on the DGX and record it here
  before tuning the retrieval step.
- The gold set has 40 questions, single-annotator. Grow it with teachers to at
  least 150 across subjects and grades for the M2 exit benchmark.
- Token counts are estimated (words plus punctuation). Calibrate against the
  embedding model's tokenizer once it is fixed.
- PDF tables are flattened (section 4).

## 7. Acceptance criteria

| Criterion | Evidence |
|---|---|
| Chunk quality reviewed on representative curriculum samples | Section 4; review sample committed with full records |
| No material loss of source context | 99.7% content retention, 100% of DOCX table rows intact, and a coverage check on every run that warns below 98% |
| Chunk boundaries and overlap configurable and versioned | Section 3.1; config hash in every chunk id; per-upload override; `test_ids_are_deterministic_and_versioned` |
| Chunk records carry complete provenance metadata | Section 3.2; provenance table in 5.2 |
| Benchmark compares v0 strategy against v1 strategy | Section 5; report committed; reproducible with two commands |
