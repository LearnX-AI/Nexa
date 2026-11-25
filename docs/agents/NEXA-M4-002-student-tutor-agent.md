# NEXA-M4-002 — Student Tutor Agent

Status: implemented (this change). The agent `nexa-student-tutor` 1.0.0 is
**proposed for review**. It runs for students once Mr. Chandana approves and
activates it (four eyes, M3-002). Owner: Yasaru.

## What it is

The **Student Tutor Agent** teaches one student at a time. It runs on the M3
agent runtime as the workflow `nexa-wf-student-tutor`, uses the M2 Knowledge
Service for the curriculum, and runs under the NEXA Teaching Intelligence
Policy (M4-001, profile `tutor`).

| | |
|---|---|
| Agent | `v1/agent_runtime/agents/proposed/nexa-student-tutor.json` (1.0.0, review) |
| Agent policy | `v1/agent_runtime/policy/agents/nexa-student-tutor.json`: learner material only; `knowledge_answer` and `calculator`; no delegation; output redacted |
| Workflow | `v1/agent_runtime/workflows/nexa-wf-student-tutor.json` (1.0.0, students only, session-aware) |
| Runtime features added | `cite_from` (the grounding guard) and conditional session writes (`orchestration/engine.py`, `workflow.py`) |
| Evaluation | `v1/tools/resilience/tutor_cases.py` (35 cases), `v1/tools/tutor_eval.py` (offline in CI; `--live` on the DGX) |
| Tests | `v1/tests/test_student_tutor.py` |

A client creates a session for the student (`POST /v1/sessions` with
`{"workflows": ["nexa-wf-student-tutor"]}`). It then runs the workflow for
each message with `{"input": {"message", "grade"}, "session_id"}`.

## Capabilities

| Capability | How |
|---|---|
| **Curriculum-grounded explanations** | The Knowledge Service gives a cited, grounded answer (as the student, so learner material only). The tutor teaches **from that answer only**, keeping its source markers. |
| **Source-aware answers** | The **grounding guard** (`cite_from`) checks every teaching reply against the curriculum answer it was built from. A reply that cites a passage the answer does not have, or drops every citation, is refused. The step then retries, and if it still fails the student gets the curriculum answer itself. Each check is traced (`agent.grounding`). |
| **Follow-up questions** | A first internal step turns a follow-up ("Why does it need light?") into a standalone question on the session's topic, so the lookup searches the right thing. The topic is kept in the student's session. |
| **Adaptive re-explanation** | "I don't understand, explain it more simply" sends the previous explanation with the request, and the tutor explains it differently: simpler words, shorter steps, a new PNG example. |
| **Guided practice and hints** | "Give me a practice question" makes one question from the curriculum answer, kept in the session. "Hint" climbs the policy's ladder one level at a time: a pointing question, then the idea, then the first step, then the full solution. |
| **Misconception detection** | When a student states something wrong, or answers a practice question, the tutor says what is not right, why it seemed reasonable, and the correct idea. Numerical practice answers are checked against the calculator's exact result, never the model's arithmetic. |
| **Age/grade-aware language** | The student's grade is passed to the Teaching Intelligence Policy: Grades 7–8, 9–10 or 11–12 style. |
| **Offline/edge-compatible** | The workflow needs only the Model Gateway, the Knowledge Service and the Tool Gateway, which all run on a school NEXA node. It makes no web calls. When the curriculum cannot be reached, it says so instead of guessing. |

## The tutoring loop

```
message ─► intent (rules)
            ├─ answer key, teacher material, assessed work, "what mark?" ─► boundary reply (no model)
            ├─ hint (practice in session)         ─► hint: next level of the ladder
            ├─ an answer (practice in session)    ─► expression ─► calculator ─► check (feedback, no mark)
            ├─ arithmetic                         ─► expression ─► calculator ─► worked answer
            └─ anything else ─► question (standalone, follow-ups resolved; internal)
                                 ─► curriculum lookup ── unreachable ─► "I won't guess"
                                      ├─ grounded + practice request ─► practice question
                                      ├─ grounded ─► explain (grounding guard) ── fails ─► the cited curriculum answer
                                      └─ not covered ─► says so; labelled general answer or "not sure"
session: topic, explanation, practice, last_hint  (each written only by the step that produces it)
```

Distress or abuse is answered by the Teaching Intelligence Policy before any
step runs (M4-001).

## Controls

| Control | How it is enforced |
|---|---|
| **Student-safe content** | Agent safety policy `nexa.safety.student/1`; the Teaching Intelligence Policy on every call; escalation before any step; personal data redacted from every output (M3-007). |
| **No teacher or admin functions** | The workflow is for the `student` role only; teachers are refused (403). The agent reads learner material only and can use only `knowledge_answer` and `calculator`. Requests for answer keys, marking guides, teacher guides, lesson plans or assessed work get a fixed reply with no model call. Another student's session is refused. |
| **No hidden grading decisions** | The tutor never gives a mark, score, grade or percentage: the prompt forbids it and the evaluation checks for it. The session holds only topic, explanation, practice and last hint. Marks and other learner-record keys are refused by the state contract (M3-006). |
| **Explicit uncertainty and abstention** | Not covered by the curriculum: it says so first, then a labelled general answer or "I'm not sure, ask your teacher". Curriculum unreachable: "I won't guess". |
| **Full traceability** | Every run records its path, knowledge status, the grounding check, the agent and policy versions, the grade band, session reads and writes, and the tool calls (M3-008). |

## Evaluation

**Offline:** 35 cases, all deterministic, run in CI on every change. They
cover these subjects and grades:

- science (Grade 7);
- geography (Grade 8);
- mathematics and social science (Grade 9);
- English and physics (Grade 10);
- biology and mathematics (Grade 11);
- chemistry (Grade 12).

| Category | Cases | What is tested |
|---|---|---|
| Grounding | 12 | One grounded explanation per subject and grade: cites only the curriculum's passages, in the right grade band. An invented citation is refused and the cited answer is used. Dropped citations are refused. A question outside the curriculum is labelled and cites nothing. Weak evidence still cites. |
| Tutoring quality | 9 | A follow-up keeps the topic. A re-explanation differs from the first and has it in context. A practice question is kept in the session. Hints climb the ladder. An answer is checked against the calculator, with no score words. A non-numerical answer is checked. A misconception is corrected. Calculations are exact. The grade sets the band (Grade 7 vs 12). |
| Role boundary | 5 | A teacher cannot run the tutor. Six teacher-only or assessed-work requests get the boundary reply with no model call. Learner material and two tools only. No marks in state. Another student's session is refused. |
| Safety | 3 | Distress reaches a person with no model call. Personal data is removed. The policy is on every call (internal steps get the internal profile). |
| Failure modes | 6 | Curriculum down: no guessing. Model down: no output. Teaching step fails: the cited answer is used. A bad expression goes to the curriculum. No session needed. A timeout, then an answer. |

**Result (offline, this change): 35/35, 0 safety invariant violations.**

**Live (DGX, after approval):** `tutor_eval --live` asks one question per
subject and grade with the real model and curriculum, then runs two full
conversations (biology Grade 11, mathematics Grade 9) through follow-up,
re-explanation, practice, two hints and an answer. It also probes the role
boundary and the safety escalation. Proposed thresholds:

| | Threshold |
|---|---|
| Grounded answers cite the curriculum | 100% |
| Answers outside the curriculum are labelled | ≥ 90% |
| Language matches the grade band | 100% |
| Tutoring moves reach the right step | ≥ 90% |
| Re-explanations differ and are not harder (sentence length) | ≥ 80% |
| Answer feedback gives no mark or score | 100% |
| Role boundary held; distress reaches a person | 100% |
| p95 response time | ≤ 120 s |
| Safety violations | 0 |

## To go live

1. Deploy this change. The agent is registered for review; nothing changes
   for users.
2. Mr. Chandana approves `nexa-student-tutor` 1.0.0, and someone other than
   the registrant activates it (registry API, four eyes).
3. Run the live evaluation on the DGX and record the results here.

## Not in this change

- **Client integration** (the Moodle plugin and EduNeX apps calling the
  tutor with the student's session): part of the client cut-over (M3-010,
  R4).
- **Running the Agent Runtime on school-edge servers.** The workflow is
  compatible, but edge servers today serve the knowledge packages only
  (M2-009).
- **Symbolic maths** (algebra, units) in practice checks: the calculator
  handles arithmetic only (M3-010, R6).
- **A model-graded tutoring-quality rubric.** The live checks are rule-based;
  a rubric graded by reviewers belongs with the M4 evaluation task.
