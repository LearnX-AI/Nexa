# NEXA-M2-008: RAG Evaluation Dataset & Retrieval Benchmarks

Status: Implemented. Fixture suite passing in CI. DGX baselines (v0 and the v1 candidate) pending the first corpus run (section 6).
Milestone: M2
Aligns with: NEXA-M2-001 (authority tiers), NEXA-M2-002 (corpus), NEXA-M2-004 (index versioning), NEXA-M2-005 (retrieval), NEXA-M2-006 (grounding), NEXA-M2-007 (school and national scope)

Implementation:
- Dataset: `v1/knowledge/benchmark/eval/dataset.v1.0.0.json`.
- Thresholds: `v1/knowledge/benchmark/eval/thresholds.v1.0.0.json`.
- Baselines: `v1/knowledge/benchmark/eval/baselines/`.
- Metrics, gate and fixture runner: `v1/knowledge/benchmark/evalsuite.py`.
- Runner: `v1/tools/nexa_eval.py`, with three suites: `fixture`, `corpus` and `live`.
- Release gate: `v1/deploy/deploy.sh`, step 2a, and the CI step "RAG evaluation gate".

Tests: `v1/tests/test_nexa_eval.py` has 13 tests. They cover:
- dataset coverage and validation;
- the metric definitions;
- the fixture gate, which catches deliberately broken authority and access rules;
- the deploy gate's refuse, allow, unchanged and override paths.

## 1. What is evaluated, and where

| Suite | Corpus and models | Where | Gates |
|---|---|---|---|
| `fixture` | 9 synthetic documents, a deterministic bag-of-words embedder and a scripted chat model | CI on every pull request; any machine | Every pull request: all 17 checks must pass |
| `corpus` | The M2-002 PNG STEM corpus (72 approved sources), real models through the Model Gateway, a scratch pgvector schema that is dropped afterwards | DGX (DEV) | Every deploy that changes retrieval code (section 5) |
| `live` | The running Knowledge Service over HTTP, including grounded answers | DEV and QA after a deploy | QA sign-off |

The fixture suite exists because the curriculum corpus cannot be made to show certain behaviour on demand:
- an official source and an LMS page that disagree;
- two schools' local notes on the same topic;
- a teacher-only answer key;
- a model that invents citations;
- a gateway that is down.

The corpus suite measures retrieval quality on real material with the real models.

## 2. The dataset (version 1.0.0)

Each item carries:
- `id`, `suite`, `type` and `question`;
- `language`, and `approved` (whether it gates);
- for corpus items: the expected `files` and `evidence` phrases, plus `subject`, `grade`, `term`, `source_type` and `format` for slicing;
- an `expect` block with the required behaviour.

A corpus hit is a retrieved chunk from an expected file that contains every evidence phrase. This is the M2-004/005 rule, so the numbers stay comparable.

| Type | Items | What it tests |
|---|---|---|
| exact_fact | 21 (20 corpus, 1 fixture) | One fact, one place (a value, a name, a definition) |
| conceptual | 20 | An explanation that spans a passage |
| ambiguous | 8 | Under-specified as a learner would ask it ("What phase is it copied in?"), with the subject and grade the tutor knows from the course |
| multilingual | 16 | Tok Pisin translations of English items. `approved=false`: reported, not gating, until a Tok Pisin speaker approves them |
| no_answer | 17 | Off-curriculum or unanswerable from the corpus (16 corpus, 1 fixture). The right behaviour is a refusal |
| conflict | 3 | Official syllabus vs LMS lesson vs external encyclopedia on the same point |
| scope | 3 | School-local vs national: a school's own notes reach its students and nobody else's |
| access | 2 | Teacher-only content never reaches students; teachers do see it |
| filters | 1 | A subject/grade filter excludes the other subjects |
| citation_integrity | 2 | A model that cites only invented sources, and one that mixes real and invented ones |
| failure | 4 | Chat model down, embedding gateway down, embedding model changed, full-text search unavailable |

Coverage across the 64 answerable corpus items (48 gating):
- subjects: Biology, Chemistry and Physics;
- grades: 11 and 12;
- terms: 1, 2 and 3;
- formats: PDF (text and scanned), DOCX and PPTX;
- source types: syllabus, student guide, teacher guide, LMS content and teacher material.

The dataset is versioned. A changed or added item means a new version: 1.1.0 for additions, 2.0.0 when existing items change meaning. The thresholds file names the dataset version it applies to.

## 3. Metrics

**Retrieval.** Measured over the gating set (approved English: exact, conceptual and ambiguous) and per slice: type, subject, grade, term, language, format and source type.
- recall@1, @5 and @10: the expected passage is at or above rank k;
- MRR@10;
- nDCG@10, with one relevant passage per question;
- precision@5: the share of the top five that come from an expected source.

**Source-authority correctness.**
- Corpus: for questions whose answer the syllabus defines (learning outcomes, and the scanned syllabus), the share where an official syllabus passage ranks first.
- Fixture:
  - an official source ranks above an LMS page on the same point, and the conflict is recorded;
  - an external reference never ranks above curriculum material;
  - a learning-outcome question gets the syllabus first.

**Citation integrity.**
- Every citation in a grounded answer must point to a passage that was given to the model.
- Fixture: invented markers are removed. An answer with nothing but invented citations becomes `ungrounded` and is not served as an answer.
- Corpus and live: the count of fabricated markers must be 0.

**Grounded-answer accuracy** (live, or corpus with `--answers`). An answer is correct when all three hold:
- its status is `grounded` or `grounded_weak`;
- it cites a passage from an expected source;
- it covers at least half the key terms of the evidence.

Also reported: the answered rate, the share citing a relevant source, the ungrounded rate and the status counts.

**No-answer behaviour.** The same evidence grading the answer endpoint uses (`grounding.assess`).
- Correct refusal: unanswerable questions graded `none`.
- False refusal: answerable questions graded `none`.

**Latency.** Retrieval p50 and p95 per question.

**Failure behaviour** (fixture):
- chat model down → `generation_unavailable`, with the passages still listed;
- embedding gateway down → HTTP 502;
- model changed under the index → HTTP 503 refusal, never wrong-space vectors;
- full-text unavailable → vector-only, reported as degraded.

## 4. Running it

```bash
# anywhere (CI runs this)
python v1/tools/nexa_eval.py fixture --out eval-report

# DGX: corpus suite, v1 candidate + v0 baseline, records the gate result for deploy.sh
python v1/tools/nexa_eval.py corpus --corpus ~/nexa-eval-corpus --out ~/nexa-evidence --record-gate

# DEV/QA: live service, with grounded answers
python v1/tools/nexa_eval.py live --base-url http://127.0.0.1:8103 --out ~/nexa-evidence
```

Each run writes a JSON and a Markdown report: gate checks, metric tables, per-item results and misses. The exit code is 0 for a pass, 1 for a fail and 2 for a setup error.

Notes on the corpus suite:
- It builds the M2-002 corpus with `knowledge_corpus.py` if `--corpus` is empty.
- It reads `NEXA_KNOWLEDGE_PG_DSN` and `NEXA_KNOWLEDGE_GATEWAY_TOKEN` from the environment.
- It never touches the live index.
- `--offline` checks the harness with stand-in embedders. It can never pass the gate.

## 5. Thresholds and the release gate

`thresholds.v1.0.0.json` (provisional until section 6 is run):

| Metric | Fixture | Corpus | Live |
|---|---|---|---|
| Fixture checks passing | 100% | | |
| Gating recall@5 | | ≥ 85 | ≥ 80 |
| Gating MRR@10 | | ≥ 0.60 | |
| exact_fact / conceptual recall@5 | | ≥ 85 each | |
| ambiguous recall@5 | | ≥ 75 | |
| Syllabus-first rate | | ≥ 0.80 | |
| Correct refusal | | ≥ 0.75 | ≥ 0.75 |
| False refusal | | ≤ 0.10 | ≤ 0.10 |
| Retrieval p95 | | ≤ 450 ms | ≤ 1000 ms |
| Answer accuracy (with answers) | | ≥ 0.70 | ≥ 0.70 |
| Fabricated citations | | 0 | 0 |

The corpus suite also compares the run with the recorded baseline for the same embedding fingerprint. These drops fail the gate:
- recall@5: more than 2.5 points;
- recall@1: more than 5 points;
- MRR@10 or nDCG@10: more than 0.05;
- syllabus-first or correct-refusal rate: more than 0.10.

A metric that was not measured fails its check; it does not pass silently.

**How a release is stopped:**
1. **Pull request (CI).** The fixture suite runs in `build-test`. A failing check fails the build, so nothing merges and nothing deploys.
2. **Deploy (`deploy.sh`, step 2a).** `deploy.sh` compares the retrieval code being deployed with what is running. The retrieval code is the git tree of `v1/knowledge` and `v1/model_gateway`, excluding the evaluation data.
   - If the code is unchanged, there is no evaluation to do.
   - If it changed, the deploy needs a passing corpus run recorded on that host for exactly that code (`~/.nexa/eval-gates/<key>.json`, written by `--record-gate`). Otherwise it stops with exit code 3 before checking anything out.
   - The key survives a merge commit that leaves the code as it was on the evaluated branch.
   - A run with uncommitted changes in those directories is never recorded.
3. **Override.** `NEXA_SKIP_EVAL_GATE=1 NEXA_SKIP_EVAL_REASON="…"` is for an emergency fix. It is logged to `.nexa_eval_gate_overrides` in `NEXA_HOME` with the commit, the key, the user and the reason.

Changing a threshold or the dataset is a reviewed change with a new version. Because the evaluation data is excluded from the key, such a change does not by itself force a new DGX run. The gate record keeps the threshold and dataset versions it passed under.

## 6. Baselines: v0 and the v1 candidate

**Offline harness check** (`evidence/m2-008-eval-corpus-offline.md`, stand-in embedders, pgvector):
- the suite builds both indexes;
- it scores all 80 corpus items;
- it slices by every dimension;
- it reports misses.

Its numbers say nothing about the real models.

**DGX corpus run.** Pending; this section is filled in from that run. Record the baseline with:

```bash
python v1/tools/nexa_eval.py corpus --corpus ~/nexa-eval-corpus --out ~/nexa-evidence --write-baseline
```

Expectation, from the M2-005 benchmark on the same corpus:
- the v1 candidate (nomic-embed with hybrid retrieval, S = 91.0) well above v0 (S = 51.0);
- bge-m3 (S = 96.0) available as a candidate pending governance sign-off.

## 7. Acceptance criteria (NEXA-M2-008)

| Criterion | Status | Evidence |
|---|---|---|
| Versioned benchmark dataset exists | **Met** | `dataset.v1.0.0.json` (97 items: 80 corpus, 17 fixture), validated by `load_dataset`; `test_dataset_is_versioned_and_covers_the_required_cases` |
| Baseline results recorded for v0 and the v1 candidate pipeline | **Pending the DGX run** (section 6) | The corpus suite measures both in one run; `--write-baseline` records them in `baselines/` |
| Regression thresholds defined | **Met** (provisional values) | `thresholds.v1.0.0.json`; section 5 |
| Evaluation runnable repeatedly in DEV/QA | **Met** | `nexa_eval.py corpus` uses a scratch schema each run and drops it; `nexa_eval.py live` is read-only |
| An M2 release cannot pass if retrieval quality regresses below thresholds | **Met** | CI fixture gate; `deploy.sh` step 2a; `test_deploy_refuses_a_retrieval_change_without_a_passing_evaluation`, `test_fixture_suite_catches_an_authority_regression`, `test_fixture_suite_catches_an_access_regression` |

## 8. Limitations

- **Gating questions are English.** The 16 Tok Pisin items are measured but do not gate until they are reviewed.
- **Answer accuracy is a proxy.** Key-term coverage plus citing the right source catches wrong and unsupported answers, but not every subtle error. A teacher review sample of live answers per release would complete it.
- **Gate records live on the deploy host.** `--record-gate` writes to `~/.nexa/eval-gates` on the host that deploys. If the CI runner deploys as a different user, set `NEXA_EVAL_GATE_DIR` for both.
- **LMS content in the corpus suite.** The corpus suite uses the M2-002 corpus, which includes 23 EduNex LMS lessons as files. Content synced later through M2-007 is covered by the live suite.
