# NEXA-M0-006: Evaluation, QA and Safety Baseline

Status: Verified against code and available QA evidence on branch nexa-v1-core
Milestone: M0
Evidence base: main.py, Prep_files/ test scripts, chat rating fields

## 1. Scope and method

Establish the measurable v0 baseline for quality, safety and QA, and separate UI
and function testing from AI quality evaluation. For each area, record the
current control, the evidence level, and the v1 action. Evidence levels:

- EVIDENCE: a test, dataset, log or measurement exists.
- ANECDOTAL: informal or manual checking only, no dataset or metric.
- NONE: no benchmark and no systematic testing.

## 2. Headline finding

NEXA v0 has no formal evaluation or QA system and no systematic safety
guardrails. There is no AI quality test suite, no guardrail or prompt injection
defense in code (no matches for guardrail, jailbreak, injection, unsafe,
sanitize in main.py), and the only structured feedback signal is a star rating
on chat logs. Quality today rests on prompt discipline (curriculum grounding and
refusal phrases) and on the deterministic SymPy maths path. This is the single
largest readiness gap for v1 and is why ADR level safety and a dedicated
Evaluation subsystem are NEW IN v1.

## 3. UI and function testing

- The only test files are experiments in Prep_files/ (retrieval_test.py,
  ocr_test.py, ocr_test_ai.py, test_easyocr.py) and a torch import probe
  (test.py). There is no automated UI or API functional test suite, no CI, and
  no regression tests.
- Functional checking to date is manual and ANECDOTAL.

## 4. AI quality and safety baseline

| Area | Current v0 control | Evidence level | v1 action |
|---|---|---|---|
| Curriculum accuracy | RAG over Chroma with syllabus-first priority; answers grounded in retrieved chunks | ANECDOTAL | Build a graded curriculum QA set per subject and grade |
| Citation and source grounding | Chunk metadata (subject, grade, doctype) surfaced; distance re-rank | ANECDOTAL | Measure citation correctness against a labelled set |
| Hallucination | Prompt discipline: grounded prompts, refusal phrases like cannot find, no information (main.py:2735) | ANECDOTAL | Hallucination rate on held-out questions, including out-of-syllabus |
| Mathematics correctness | SymPy exact compute for solve, simplify, integrate, differentiate (main.py:2337 onward) | EVIDENCE (deterministic for covered ops), no test set | Maths correctness suite incl. cases SymPy does not cover |
| Teacher and student role behaviour | Role inferred from email domain, shapes answer via build_role_instruction (main.py:3642) | ANECDOTAL | Role behaviour tests per role, including privilege boundaries |
| Inappropriate content handling | Minimal: one inappropriate reference, no moderation layer | NONE | Content safety classifier and policy in Safety subsystem |
| Prompt injection and jailbreak resistance | None in code (no injection or jailbreak handling) | NONE | Injection and jailbreak red-team set, measured pass rate |
| Image generation safety | Remote Qwen on the Ilaibu DGX; no safety filter observed in the service path | NONE | Image prompt and output safety checks before return |
| Latency | No measurement in v0 (no trace, no metrics) | NONE | Per-endpoint latency targets, measured via Observability traces |
| Failure handling | try and except around model, DB and ingest with fallbacks (for example OpenAI then local) | EVIDENCE (code paths), no test | Fault-injection tests for model, DB and image outages |
| Offline and sync behaviour | Not applicable to v0 service; edge is NEW IN v1 (ADR-0007) | NONE | Define once NEXA Mini exists |
| English and Tok Pisin | Language handling referenced (about 18 sites), English is the working language | ANECDOTAL | Decide Tok Pisin scope, then build a small bilingual set |

## 5. Feedback and signal available today

- Star ratings on nexa_chat_logs (stars column) are the only structured user
  feedback. They are collected but there is no evidence they are analysed as a
  quality metric.
- Chat logs themselves are a corpus that can seed evaluation sets, subject to
  the learner-data privacy handling in NEXA-M0-004 and NEXA-M0-005.

## 6. What has evidence, what is anecdotal, what has no benchmark

- EVIDENCE (mechanism, not a metric): maths determinism via SymPy, failure
  fallback paths, star ratings collected.
- ANECDOTAL: curriculum accuracy, grounding and citations, hallucination
  control, role behaviour, language behaviour.
- NONE: inappropriate content, prompt injection and jailbreak, image safety,
  latency, offline and sync, and any automated functional or regression testing.

## 7. Prioritized benchmark and test backlog for v1

1. Safety red-team set: prompt injection, jailbreak, inappropriate content and
   role-escalation attempts, with a measured pass rate gate. Highest priority
   because it is currently NONE and the system faces students.
2. Curriculum QA set: graded questions per subject and grade with expected
   answers and expected sources, to measure accuracy and citation correctness.
3. Hallucination set: in-syllabus and deliberately out-of-syllabus questions to
   measure refusal versus fabrication.
4. Mathematics correctness suite: covered and uncovered operations, with LaTeX
   output checks.
5. Role behaviour tests: student versus teacher outputs and privilege
   boundaries, tied to the v1 Identity roles.
6. Image safety checks: prompt and output screening before an image is returned.
7. Latency and failure tests: per-endpoint latency targets measured from
   Observability traces, plus fault injection for model, database and image
   outages.
8. Language set: a small English and Tok Pisin set once Tok Pisin scope is
   decided.

## 8. Ownership and v1 home

- Owner: AI engineering with security review.
- v1 home: the Safety and Evaluation service (NEXA-M1-001 section 3), fed by
  Observability traces and by curated datasets. Regression gates run in CI
  before any model, prompt or retrieval change ships.

## 9. Verification status

| Item | Status |
|---|---|
| No automated functional or AI test suite | VERIFIED |
| No guardrail, injection or jailbreak handling in code | VERIFIED |
| SymPy deterministic maths for covered ops | VERIFIED |
| Grounding and refusal phrasing present | VERIFIED |
| Star ratings collected as the only feedback | VERIFIED |
| Latency, image safety, offline measured | NOT IMPLEMENTED |
