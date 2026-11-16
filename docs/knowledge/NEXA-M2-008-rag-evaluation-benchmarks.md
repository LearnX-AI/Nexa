# NEXA-M2-008: RAG Evaluation Dataset & Retrieval Benchmarks

Status: Complete. Fixture suite passing in CI; v0 and v1 baselines recorded on the DGX (final run 2026-11-15, section 6); corpus and live (DEV) gates passing; thresholds 1.4.0 final.
Milestone: M2
Aligns with: NEXA-M2-001 (authority tiers), NEXA-M2-002 (corpus), NEXA-M2-004 (index versioning), NEXA-M2-005 (retrieval), NEXA-M2-006 (grounding), NEXA-M2-007 (school and national scope)

Implementation:
- Dataset: `v1/knowledge/benchmark/eval/dataset.v1.2.0.json` (earlier versions kept for history; 1.1.0 added the general-answer items x18 to x20; 1.2.0 adds spelling and answer-quality fixture items x21 to x25 and fixture document F10, and 10 corpus questions with a misspelt word, type `spelling`, reported and gated separately from the 48-question gating set).
- Thresholds: `v1/knowledge/benchmark/eval/thresholds.v1.4.0.json`, final, set from the DGX corpus and live runs of 2026-11-15 (earlier versions kept for history).
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

A hit is a retrieved passage that contains every evidence phrase (the phrases are specific: a value, a name, a defining sentence), from any approved source. The first live (DEV) run on 2026-11-15 showed why: the same EduNex lesson is synced from several Moodle courses (lesson notes, teacher note, PPT), and the stricter M2-004/005 rule, which also required the exact file named in the dataset, counted the right content in another copy as a miss. That stricter rule is still reported as `source_recall@5`. recall@k, MRR and nDCG use the passage rule from dataset 1.2.0 / evaluation code of 2026-11-15 on; earlier reports used the file rule.

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

`thresholds.v1.0.0.json`, the first provisional values (the current values are in `thresholds.v1.4.0.json`, section 6):

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

**DGX corpus run, 2026-11-11** (commit `0f43e0e`, nomic-embed-text, pgvector, 72 sources / 1811 chunks; `evidence/m2-008-eval-corpus-dgx-20261111.md`, baseline `baselines/corpus-nomic-embed.json`):

| Gating set (48 approved English questions) | v0 baseline | v1 candidate | Change |
|---|---|---|---|
| recall@1 | 27.1 | 54.2 | +27.1 |
| recall@5 | 56.2 | 95.8 | +39.6 |
| recall@10 | 58.3 | 97.9 | +39.6 |
| MRR@10 | 0.373 | 0.706 | +0.333 |
| nDCG@10 | 0.424 | 0.774 | +0.350 |
| precision@5 | 0.583 | 0.633 | +0.050 |

v1 by type, recall@5: exact fact 95.0, conceptual 100.0, ambiguous 87.5, Tok Pisin (not gating) 81.2. Syllabus-defined questions answered from the official syllabus first: 6/6. Retrieval latency p50 78 ms, p95 105 ms. Answerable questions wrongly refused: 0/48.

**Refusal is two layers, and is gated on both.** The evidence grade (similarity and word coverage) refused 8 of 16 unanswerable questions (0.50): it is tuned to never refuse an answerable one (0/48). The other 8 reach the answer model, which must reply INSUFFICIENT_EVIDENCE when the passages do not answer (as it did live for the bird-count question asked as a student). So the corpus gate keeps the evidence-grade rate as a regression floor (≥ 0.40, at most 0.10 below baseline), and the end-to-end check (`--answers`, and the live suite) requires that an unanswerable question gets a curriculum answer at most 10% of the time (`no_answer.curriculum_answer_rate`); a labelled general answer or the refusal both count as correct.

**Thresholds 1.2.0**, set from this run: gating recall@5 ≥ 92.5, MRR@10 ≥ 0.65, exact ≥ 90, conceptual ≥ 92.5, ambiguous ≥ 75, syllabus-first ≥ 0.90, plus the regression limits against the baseline in section 5.

**First live (DEV) run, 2026-11-15** (commit `7e1bdd1`, the running Knowledge Service with the M2-002 corpus and 473 synced EduNex items): answers 0.759 accurate, 0 fabricated citations, end-to-end refusal 1.0, false refusal 0.0, p95 100 ms; gating recall@5 79.2 under the file rule, below the live floor of 80. The drop was in EduNex lessons (60.9 vs 91.3 in the corpus run): copies of the same lesson from other Moodle courses filled the top results (precision@5 0.40) and were counted as misses. Two changes followed: result diversity in retrieval (at most two passages per source, near copies dropped; NEXA-M2-005 step 6) and the passage rule above.

### Final baseline, 2026-11-15

**Corpus suite** (commit `9b6c06c`, gate key `a394165cff58f30f`, dataset 1.2.0, passage scoring, near-copy removal; `evidence/m2-008-eval-corpus-dgx-20261115.md`, `baselines/corpus-nomic-embed.json`). Gate PASS.

| Gating set (48 approved English questions) | v0 | v1 |
|---|---|---|
| recall@1 | 29.2 | 58.3 |
| recall@5 | 56.2 | 95.8 |
| recall@10 | 58.3 | 97.9 |
| MRR@10 | 0.391 | 0.727 |
| nDCG@10 | 0.438 | 0.790 |
| source_recall@5 (expected file) | 56.2 | 93.8 |
| Syllabus first, syllabus-defined questions | 0.33 | 1.00 |

v1 recall@5 by slice: exact fact 95.0, conceptual 100, ambiguous 87.5, misspelt questions 100 (v0 70), Tok Pisin (not gating) 81.2 (v0 37.5); EduNex lessons 91.3 (v0 26.1). Grounded answers: accuracy 0.875 (48), cites an expected source 0.896, 0 fabricated citations, 0 ungrounded. Unanswerable questions: evidence grade refuses 0.50, end to end 1.00 (no curriculum answer); answerable wrongly refused 0.00. Latency p50 74 ms, p95 101 ms.

**Live suite** against the deployed DEV service (same commit; corpus plus 473 synced EduNex items; `evidence/m2-008-eval-live-dgx-20261115.md`). Gate PASS: gating recall@5 83.3, recall@10 93.8, MRR 0.644, answer accuracy 0.776, 0 fabricated citations, end-to-end refusal 1.0, false refusal 0.0, p95 111 ms. Live is lower than the corpus run because the live index also holds the EduNex lessons of other Moodle courses (for example Grade 11 Biology, course 11), which compete for the top results; Biology is the weakest live slice (recall@5 69.0, recall@10 89.7) and is the first target for retrieval work in M3.

**What changed between the 2026-11-11 and 2026-11-15 runs.** Spelling correction, course outlines, honest confidence and general answers (answer path only); passage scoring; near-copy removal in retrieval. A per-source cap of two passages was also tried and refused by the gate (recall@5 95.8 to 81.2, commit `7a174fd`); it is off by default. That refusal is the release gate working as intended.

**Thresholds 1.4.0 (final).** Corpus: gating recall@5 ≥ 92.5, MRR@10 ≥ 0.65, exact ≥ 90, conceptual ≥ 92.5, ambiguous ≥ 75, misspelt ≥ 90, syllabus-first ≥ 0.90, evidence-grade refusal ≥ 0.40, false refusal ≤ 0.10, p95 ≤ 450 ms; with answers, accuracy ≥ 0.70, valid citations ≥ 0.95, fabricated = 0, ungrounded ≤ 0.05, curriculum answers to unanswerable questions ≤ 0.10; and no metric more than its allowed drop below the baseline (recall@5 2.5 points, recall@1 5 points, MRR and nDCG 0.05, rates 0.10). Live: recall@5 ≥ 80, recall@10 ≥ 90, accuracy ≥ 0.70, end-to-end refusal ≥ 0.90, false refusal ≤ 0.10, fabricated = 0, p95 ≤ 1000 ms.

## 7. Acceptance criteria (NEXA-M2-008)

| Criterion | Status | Evidence |
|---|---|---|
| Versioned benchmark dataset exists | **Met** | `dataset.v1.2.0.json` (122 items; 1.0.0 and 1.1.0 kept), validated by `load_dataset`; `test_dataset_is_versioned_and_covers_the_required_cases` |
| Baseline results recorded for v0 and the v1 candidate pipeline | **Met** | Section 6: DGX run 2026-11-15, v1 recall@5 95.8 vs v0 56.2, MRR 0.727 vs 0.391; `baselines/corpus-nomic-embed.json` |
| Regression thresholds defined | **Met** | `thresholds.v1.4.0.json` (final), set from the corpus and live baselines; sections 5 and 6 |
| Evaluation runnable repeatedly in DEV/QA | **Met** | Six corpus runs and two live runs on the DGX between 2026-11-11 and 2026-11-15; `corpus` uses a scratch schema each run and drops it; `live` is read-only against the running service |
| An M2 release cannot pass if retrieval quality regresses below thresholds | **Met**, and shown on the DGX: `deploy.sh` refused `0f43e0e` until its evaluation passed, and the gate refused `7a174fd` when its retrieval change lowered recall@5 from 95.8 to 81.2 | CI fixture gate; `deploy.sh` step 2a; `test_deploy_refuses_a_retrieval_change_without_a_passing_evaluation`, `test_fixture_suite_catches_an_authority_regression`, `test_fixture_suite_catches_an_access_regression` |

## 8. Limitations

- **Gating questions are English.** The 16 Tok Pisin items are measured but do not gate until they are reviewed.
- **Answer accuracy is a proxy.** Key-term coverage plus citing the right source catches wrong and unsupported answers, but not every subtle error. A teacher review sample of live answers per release would complete it.
- **Gate records live on the deploy host.** `--record-gate` writes to `~/.nexa/eval-gates` on the host that deploys. If the CI runner deploys as a different user, set `NEXA_EVAL_GATE_DIR` for both.
- **LMS content in the corpus suite.** The corpus suite uses the M2-002 corpus, which includes 23 EduNex LMS lessons as files. Content synced later through M2-007 is covered by the live suite.

## 9. Answer quality: what is measured, separately

Each axis is a separate number in every corpus (`--answers`) and live report.

| Axis | Metric | Gated |
|---|---|---|
| Factual accuracy | `answers.accuracy`: answered, cites an expected source, covers at least half the evidence's key terms; `mean_key_coverage` | Yes, ≥ 0.70 |
| Citation correctness | `citation_valid_rate` (answered with a citation that points to a passage the model was given); `cites_relevant_rate` (cites an expected source); `citation_precision` (share of the cited passages that come from an expected source; a lower bound, since another passage can also be correct) | Valid rate ≥ 0.95 (corpus); precision reported |
| Hallucination | `fabricated_citations` (citation markers to passages that were not given); `ungrounded_rate` (answers whose only citations were invented, withheld); `unsupported_answer_rate` and `weak_sentences_total` (sentences weakly supported by the passage they cite, and paragraphs no passage supports) | Fabricated = 0, ungrounded ≤ 0.05; unsupported content reported until a baseline sets its limit |
| No-answer handling | `correct_refusal_rate` (evidence grade), `end_to_end_refusal_rate` / `curriculum_answer_rate` (an unanswerable question never gets a curriculum answer), `false_refusal_rate` (answerable questions refused) | Yes (section 6) |
| Retrieval | recall@k, MRR, nDCG, precision@5, source recall, syllabus-first | Yes |

Limits: accuracy and the unsupported-content measures are automatic proxies based on word overlap. They catch
wrong-source, missing-content and invented-citation answers, but not every subtle factual error. A teacher
review of a sample of live answers per release (for example 30 answers, rated correct / partly correct /
wrong, and supported / not supported by the cited passage) is the planned complement (section 8).

## 10. Extending the evaluation to more grades, subjects and languages

Nothing in the framework is specific to Grades 11–12 or to the three sciences:
- every item carries `subject`, `grade`, `term`, `language`, `format` and `source_type`, and every report is
  sliced by all of them automatically;
- the thresholds file can set a floor for any slice, without code changes, for example
  `"retrieval.slices.subject=Mathematics.recall@5": 85.0` or `"retrieval.slices.language=tpi.recall@5": 70.0`;
  a slice with no measured items fails rather than passing silently
  (`test_thresholds_can_gate_a_single_subject_grade_or_language`).

Adding a subject or grade:
1. Ingest its approved sources on core NEXA (NEXA-M2-002), with `subject` and `grade` set in the source metadata.
2. Subject teachers write the items: at least 20 answerable questions per subject and grade (exact,
   conceptual, ambiguous, misspelt), and at least 5 unanswerable ones, each with its expected source and
   evidence phrases. A second teacher reviews them.
3. Release them as a new dataset version (1.x.0 for additions).
4. The first run reports the new slice. After review of that run, a new thresholds version adds its floor
   and the baseline is re-recorded on the DGX.

Adding a language: items are added with `approved: false`. They are measured and reported (as the 16 Tok
Pisin items are today) but do not gate until a speaker of the language approves the wording and the expected
behaviour. Approval sets `approved: true` in a new dataset version, and a language slice floor is added.

Scaling: the gating set is 48 questions today. With each subject and grade adding about 25 items, the corpus
suite's run time grows in proportion to the number of items. Above a few hundred items, the
corpus suite can run a fixed, stratified gating subset on every deploy and the full set nightly.

## 11. Enforcement across environments

| Stage | What is enforced | Where |
|---|---|---|
| Pull request to `main` | Unit and integration tests and the fixture evaluation (25 checks) | CI `build-test` |
| Deploy to DEV (DGX) | Corpus evaluation for exactly the retrieval code being deployed, recorded on the host, or the deploy stops | `deploy.sh` step 2a |
| Promote to QA | The same `deploy.sh` and gate, on the QA host, with its own recorded evaluation; QA approval in the `qa` GitHub environment | CI `promote-qa` |
| School servers | M2-008 live suite against the school server before it is put in service | Manual, `NEXA-M2-008-reproduce.md` section 4 |

Open items, as of 2026-11-16:
- `main` has no branch protection, so a pull request with a failing evaluation can still be merged by hand.
  Required status check `build-test` on `main` closes this (repository setting).
- The CI deploy jobs run on pushes to `nexa-v1-core`, while work is now merged to `main`; DEV deploys are run
  with `deploy.sh` on the DGX, where the gate applies. Pointing the deploy jobs at `main` (with
  `NEXA_DEPLOY_ENABLED`) makes DEV and QA deploys automatic and gated on every merge.
- A separate QA host and its `nexa-qa` runner are not yet in use.
- The live suite runs after a deploy by hand; it is not yet part of `deploy.sh`.

## 12. Reproducing the results

Step by step, for a developer who has not run it before: [NEXA-M2-008-reproduce.md](NEXA-M2-008-reproduce.md).
