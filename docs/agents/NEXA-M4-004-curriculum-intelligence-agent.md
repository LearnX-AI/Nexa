# NEXA-M4-004: Curriculum Intelligence Agent

Status: **implemented, awaiting the DGX evaluation**. Built to the findings
approved on 27 Nov 2026 (`NEXA-M4-004-curriculum-intelligence-findings.md`).
Owner: Yasaru.

## What it is

The **Curriculum Intelligence Agent** makes the curriculum navigable for
teachers and curriculum reviewers. It runs on the M3 agent runtime as the
workflow `nexa-wf-curriculum-intelligence`, under the NEXA Teaching
Intelligence Policy (M4-001, profile `planning`). It is read only: nothing is
published or changed.

| Task | What it returns |
|---|---|
| `map` | Where a question or a piece of teaching content sits: grade, subject, unit, topic and the **official learning outcomes**, with sources and a confidence |
| `alignment` | The teacher's own material against what the curriculum requires for its topic: Covered, Partly covered, Not covered, Suggestions, with citations |
| `progression` | Prerequisites, what the topic covers, and what comes next, with citations |

**How a client calls it:** `POST /v1/workflows/nexa-wf-curriculum-intelligence/runs`
with `task`, `text` (the question, content or lesson), `grade` and `subject`.
Use `any` for a grade or subject that is not known; the mapping then finds it.

## Components

| Part | Where |
|---|---|
| Agent | `v1/agent_runtime/agents/proposed/nexa-curriculum-intelligence.json` (1.0.0, review) |
| Agent policy | `v1/agent_runtime/policy/agents/nexa-curriculum-intelligence.json`: learner and teacher-only curriculum through `curriculum_map` and `knowledge_answer`; no writes, no memory, no delegation, no learner records |
| Workflow | `v1/agent_runtime/workflows/nexa-wf-curriculum-intelligence.json` (1.0.0, teachers and school admins) |
| New tool | `curriculum_map` (Tool Gateway, `v1/tool_gateway/tools/curriculum_map.json`, handler in `handlers.py`), for this agent only |
| Knowledge Service | Grade guard and premise correction (`v1/knowledge/main.py`, `grounding.py`; flags `NEXA_GRADE_GUARD`, `NEXA_PREMISE_CORRECTION`); `GET /v1/knowledge/lms/items` |
| Re-scope tool | `v1/tools/lms_rescope.py`: moves the EduNeX courses from the test school to POMNSOE (approved; dry run by default) |
| Outcome registry | `v1/tools/outcome_registry.py`: draft of every outcome code in the official syllabi, as a review sheet for CDD |
| Evaluation | `v1/tools/resilience/curriculum_cases.py` (22 cases), `v1/tools/curriculum_eval.py` (offline in CI; `--live` on the DGX), the demo pack `v1/tools/curriculum_demo.py` |
| Tests | `v1/tests/test_curriculum_intelligence.py`, `v1/tests/test_curriculum_guards.py` |

## How mapping works (`curriculum_map`)

```
text, grade, subject ─► grade = given, else named in the text ("Grade 7")
   └► Knowledge Service query, filtered to that grade and subject (as the user)
        ├ nothing ─────────────► not_covered: "NEXA holds no approved Grade 7 … material"; nothing substituted
        ├ subjects disagree ───► ambiguous: asks which subject and grade
        └ found ─► subject and grade by agreement of the top sources; unit and topic from their curriculum position
                    └► a second query, official syllabus only ─► outcome codes of that grade (rule R3)
                         confidence high: an official outcome found and the sources agree (≥ 80%); else low, with a note
```

- **No grade substitution:** a grade that is given or named is a hard filter. A grade NEXA does not hold is `not_covered`.
- **Outcomes only from the official syllabus:** codes in lesson notes or LMS items are never offered as outcomes, and codes of another grade are dropped.
- **Advisory:** every answer ends with a note that it is for teacher and reviewer use, with the run id, and that nothing was published.

## Knowledge Service changes (all agents benefit)

| Change | Why (demo, 27 Nov) | Behaviour |
|---|---|---|
| Grade guard | A Grade 7 English question was answered from Grade 11 material | A question that names a grade is answered only from that grade, unless the request sets its own grade filter; no material means no answer, no model call, and a note |
| Premise correction | False premises were refused 0/5 although the right passages were found | Strong evidence and a model refusal lead to a second prompt; a **cited** correction is returned with a note; otherwise the refusal stands; an uncited "correction" is withheld |
| LMS item listing | Re-scoping needs the items' last events | `GET /v1/knowledge/lms/items`, LMS administrators only |

Both behaviours have a switch, default on, and ship only through the M2-008
release gate (`deploy.sh` refuses a `v1/knowledge` change without a passing
corpus evaluation). The Teacher Assistant's fallback lookup now keeps the
grade (workflow 1.2.0), so the guard applies to it too.

## Re-scoping the EduNeX courses to POMNSOE

The EduNeX courses (466 items) were synced under the test school
`edunex-core-test`, so POMNSOE users could not see their own courses (demo:
4/12 covered as a POMNSOE teacher, 10/12 with the content visible).
`lms_rescope` replays each active item's last sync event, unchanged except
for its id and time, as an administrator of POMNSOE, through the normal sync
endpoint. The Knowledge Service re-governs and re-indexes each item; every
replayed event is stored and audited.

- It is a **dry run** unless `--apply` is given.
- It moves only school-scoped items of the given school. Content shared with an item that is not moved is left unchanged, because identical content is stored once.
- Running it again shows 0 left to move. **To undo**, run it with the schools swapped.

## Controls

| Control | How |
|---|---|
| No silent substitution | Grade given or named is a hard filter in both the tool and the Knowledge Service; `not_covered` is a fixed reply with no model call |
| Official outcomes only (R3) | Outcome codes come from a separate official-syllabus query, filtered to the mapped grade |
| Grounding | Alignment and progression cite only the curriculum answer's passages (`cite_from` guard); an invented citation fails the run with no output; no curriculum text means mapping only |
| Least privilege | Read only; the tool is allowed for this agent only; students cannot run the workflow (403); contact details in the text are refused before any call |
| No publication | Nothing is written; the re-scope is a separate, approved, audited operation |
| Policy | The Teaching Intelligence Policy is on every model call (profile `planning`) |

## Evaluation

**Offline (CI): 22/22, 0 safety invariant violations.**

| Category | Cases | What is tested |
|---|---|---|
| Mapping | 8 | Grade, subject, unit, topic and official outcomes; grade named in the text; given grade and subject filter the search; a grade or subject NEXA does not hold is not covered and not substituted; outcomes only from the official syllabus; no outcomes of another grade; mixed subjects are ambiguous |
| Grounding | 5 | Alignment and progression grounded with all sections and citations; lookups name the mapped grade; an invented citation stops the answer; no curriculum text gives the mapping only |
| Boundary | 4 | Students refused; contact details refused before anything runs; read only and least privilege; other agents cannot use `curriculum_map` |
| Safety | 2 | Curriculum down: nothing mapped; the policy is on every call |
| Integration | 3 | Advisory note with the run id; unknown task explained; through the Agent Runtime's HTTP API |

**Live (DGX, after deployment, re-scope and approval):** `curriculum_eval --live`
as a POMNSOE teacher. Proposed thresholds:

| | Threshold |
|---|---|
| Covered questions mapped to the right grade and subject (grade and subject left as `any`) | ≥ 90% |
| Grades 7–10 and other subjects not covered, never substituted | 100% |
| Outcome codes only of the mapped grade | 100% |
| Alignment and progression grounded and cited | ≥ 90% |
| p95 seconds per run | ≤ 180 |
| Safety violations | 0 |

The curriculum demo pack is rerun as a POMNSOE teacher (target: covered
≥ 10/12 as typed, 12/12 with grade and subject, incorrect premises corrected,
no substitution), and the M2-008 live benchmark as a POMNSOE learner.

## To go live

1. Run the M2-008 corpus gate on the DGX, then deploy (the gate is required for the `v1/knowledge` change).
2. Re-scope the EduNeX courses to POMNSOE: dry run, then `--apply` (approved).
3. Mr. Chandana approves `nexa-curriculum-intelligence` 1.0.0, and it is activated.
4. Run `curriculum_eval --live`, the demo pack and M2-008 as POMNSOE; record the results here.
5. CDD validates the outcome registry draft and signs the demo sheets.

## Not in this change

- **A stored prerequisite graph.** Progression is read from the curriculum text each time. A validated registry of prerequisite links can follow once CDD has validated the outcomes.
- **Grades 7–10 and other subjects.** They are not available; NEXA says so. They are added through the existing ingestion pipeline (or the S3 adapter) when digitized.
- **An S3 connector.** The S3 folder holds content NEXA already has (findings, pilot 24/31), so it is not needed now.
