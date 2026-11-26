# NEXA-M4-003 — Teacher Assistant Agent

Status: implemented (this change). The agent `nexa-teacher-assistant` 1.0.0
is **proposed for review**. It runs for teachers once Mr. Chandana approves
and activates it (four eyes, M3-002). Owner: Yasaru.

## What it is

The **Teacher Assistant Agent** drafts planning and assessment material for
teachers. It runs on the M3 agent runtime as the workflow
`nexa-wf-teacher-assistant` and uses the M2 Knowledge Service, including
teacher guides. It runs under the NEXA Teaching Intelligence Policy (M4-001,
profile `planning`).

**Every draft waits for the teacher's review.** An approved draft is
returned as a document with its curriculum sources and a provenance record.

| | |
|---|---|
| Agent | `v1/agent_runtime/agents/proposed/nexa-teacher-assistant.json` (1.0.0, review) |
| Agent policy | `v1/agent_runtime/policy/agents/nexa-teacher-assistant.json`: learner and teacher-only curriculum; `knowledge_answer` and `teaching_document`; no learner records, no memory, no delegation; output redacted |
| Workflow | `v1/agent_runtime/workflows/nexa-wf-teacher-assistant.json` (1.1.0, teachers and school admins) |
| New tool | `teaching_document` (Tool Gateway, built in): the draft as a document with sources and provenance; refuses contact details; stores nothing |
| Runtime | `{{run.id}}` and `{{run.workflow}}` in templates, for provenance |
| Evaluation | `v1/tools/resilience/teacher_cases.py` (43 cases); `v1/tools/teacher_eval.py` (offline in CI; `--live` on the DGX) |
| Tests | `v1/tests/test_teacher_assistant.py` |

**How a client calls it:** `POST /v1/workflows/nexa-wf-teacher-assistant/runs`
with:

- `task`: `lesson_plan`, `differentiation`, `quiz`, `rubric`, `resources`,
  `alignment` or `student_support`;
- `topic`;
- `grade`;
- `details`: the teacher's notes. For an alignment check, this is their own
  lesson. For student support, it describes the need, without names.

Then `POST /v1/workflow-runs/{id}/decision` to approve or reject.

## Capabilities

| Capability | Task | What the draft contains |
|---|---|---|
| Lesson planning | `lesson_plan` | Learning objectives, Materials (low-cost, PNG schools), Lesson steps with minutes, Differentiation, Check for understanding, Curriculum links |
| Differentiation | `differentiation` | Support, Core and Extension activities; activities are described, students are not labelled |
| Quiz and question generation | `quiz` | Questions (mixed types, number from the notes or 10) and an Answer key with reasons and sources, for the teacher only |
| Rubric and assessment drafting | `rubric` | A criteria × four-level table (Beginning to Advanced) and How to use it; no marks for any student's work |
| Teaching-resource suggestions | `resources` | 5 to 8 items, each marked as a suggestion, free or low-cost and local; Curriculum links |
| Curriculum alignment checks | `alignment` | The teacher's own material against the curriculum: Covered (with sources), Gaps, Suggestions |
| Student-support suggestions | `student_support` | From the teacher's description only: Suggested strategies, Check progress, When to involve others; no diagnosis and no decision |
| Document generation through approved tools | all | `teaching_document`: title, task, grade, status line, the draft, Curriculum sources, Provenance (agent, policy, run, content digest), then the teacher's approval and note |

The flow:

```
intent ── grading, ranking, discipline ─► refused (no model)
        ├ contact details in the notes ──► refused (no model)
        ├ unknown task ──────────────────► the list of tasks
        └ task ─► curriculum (teacher material included) ── not covered / unreachable ─► no draft
                   └ grounded ─► draft (grounding guard) ─► document ─► teacher review
                                                                     ├ approve ─► document + approval
                                                                     └ reject  ─► discarded
```

## Controls

| Control | How it is enforced |
|---|---|
| **No unauthorized access to learner data** | The agent has no learner-memory scopes, no session, no write permission and no delegation. Its only tools are the curriculum and the document tool. It works only from what the teacher types, and contact details in the notes are refused. The document tool refuses contact details too, and personal data is redacted from every draft. Teacher-only curriculum is granted to this agent, never to the Student Tutor. Students cannot run the workflow (403), and documents are typed `teacher_only`. |
| **Suggestions remain teacher-reviewable** | Every draft stops at a review checkpoint and nothing is released before the teacher approves it. Reject discards it. A draft not decided in 7 days expires. Every draft starts "DRAFT for teacher review", and the document states it is AI-assisted. |
| **No autonomous high-stakes grading or discipline** | Requests to mark work, rank students, decide who fails, or suspend, expel or discipline get a fixed refusal with no model call. Rubrics describe levels and never mark a student. Student support never diagnoses or decides. |
| **Full provenance and audit trail** | Each document carries its curriculum sources, the agent, the policy, the run id, a content digest, and the reviewer and their note. The run trace records the knowledge call, the agent call with its policy and grade band, the grounding check, the tool call, and the review decision (M3-008). |
| **Curriculum grounding** | No draft without curriculum evidence. The grounding guard refuses a draft that cites passages the curriculum answer does not have, or that drops every citation; the run then fails and nothing is drafted. |

## Evaluation

**Offline:** 41 deterministic cases, run in CI on every change. They cover
every task, and lesson plans for:

- English (Grade 7);
- geography (Grade 8);
- mathematics (Grade 9);
- chemistry (Grade 10);
- biology (Grade 11);
- physics (Grade 12).

| Category | Cases | What is tested |
|---|---|---|
| Role boundary | 6 | Students refused. Grading, ranking and discipline refused with no model call. Contact details refused. No learner-data access (tools, memory, session, write). Teacher material is for teachers only. Another school cannot review. |
| Curriculum grounding | 11 | All 7 tasks cite only the curriculum's passages. An invented citation stops the draft. A topic not in the curriculum gets no draft. Curriculum down: no draft. Teacher material is searched. |
| Usefulness | 15 | All 7 tasks have their expected sections and become a document. Lesson plans in 6 subjects and Grades 7–12 get the right grade band. Answer keys never reach students. The alignment check uses the teacher's own material. |
| Safety | 4 | Everything waits for the teacher, and reject discards. Distress in the notes goes to child protection with no model call. Contact details are removed from drafts. The policy is on every call. |
| Integration | 5 | End to end: the document has sources, provenance and the approval. The audit trail is complete. Another teacher may review. Runs work through the Agent Runtime's HTTP API, and students are refused there too. Unknown tasks are explained. |

**Result (offline): 43/43, 0 safety invariant violations.**

The M3-009 invariant V8 (the initiator never decides their own run) now
applies to four-eyes checkpoints only. Here, a teacher reviewing their own
draft is the intended control.

**Live (DGX, after approval):** `teacher_eval --live` runs all 7 tasks on
photosynthesis (Grade 11) and approves each draft. It also runs lesson plans
in 6 subjects and grades, and the boundary and safety probes. Proposed
thresholds:

| | Threshold |
|---|---|
| Drafts reach the teacher's review | ≥ 90% |
| Documents have the expected sections (at least 80% of them) | ≥ 90% |
| Documents cite the curriculum | 100% |
| Documents carry provenance and the approval | 100% |
| Language matches the grade band | 100% |
| Role boundary and safety probes held | 100% |
| p95 time to a draft | ≤ 180 s |
| Safety violations | 0 |

## First live run on the DGX (26 Nov 2026) and the fixes

The agent was activated and `teacher_eval --live` run.

**What passed:**

- role boundary and safety: 5/5 (students refused, no grading, no discipline, no contact details, distress to child protection);
- grade bands: 100%;
- safety violations: 0;
- p95: 24.5 s.

**What did not pass:** only 3 of the 7 task drafts completed.

| Problem | Cause | Fix (workflow 1.1.0) |
|---|---|---|
| differentiation, quiz and alignment: "not grounded" | The same lookup ("Photosynthesis (Grade 11)") came back `grounded`, `grounded_weak` or `no_evidence` from run to run. A bare topic is a weak query. | The lookup asks a full question ("What are the key ideas, terms and learning outcomes for … in Grade …?"). If that is not grounded, it asks once more with the plain topic (`fallback_question`). Both answers are traced. |
| rubric: failed | The grounding guard refused the draft. Its retry used the same idempotency key, so it **replayed the refused reply** instead of asking again. This affected every `cite_from` retry, the Student Tutor's included. | Each retry after a refusal has its own key and **tells the model why it was refused** (the markers it may use). Drafts get 3 attempts. |
| resources: sections not found | The model wrote its own headings, because the prompts mixed headings with descriptions. | Each prompt lists the exact headings to use. The live check also accepts `###` and bold headings. |
| Lesson plans for mathematics (Grade 9), geography (Grade 8) and English (Grade 7) | Those grades and subjects are not in the indexed curriculum. | None: no draft without the curriculum is the required behaviour. They are reported as such. |

The lookup's variance on an identical query is reported to the Knowledge
Service owners (M2): the same topic should not move between `grounded` and
`no_evidence`.

## To go live

1. Deploy this change. The agent is registered for review; nothing changes
   for users.
2. Mr. Chandana approves `nexa-teacher-assistant` 1.0.0, and it is
   activated.
3. Run `teacher_eval --live` on the DGX and record the results here.

## Not in this change

- **Word or PDF export.** The document is Markdown, which the client can
  render or convert. A file-export tool is a later addition through the
  Tool Gateway.
- **Class data** (a class list, results) to tailor drafts. That needs the
  learner-data access model of NEXA-GOV-001 section 4 to be approved first.
- **Client integration** into Moodle and EduNeX (M3-010, R4).
