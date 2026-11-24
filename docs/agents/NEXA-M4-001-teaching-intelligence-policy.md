# NEXA-M4-001 — Shared Teaching Intelligence & Pedagogy Policy

Status: implemented (this change). Policy **1.0.0, proposed**, for approval
by Mr. Chandana. Owner: Yasaru.

## What it is

The **NEXA Teaching Intelligence Policy** is one versioned policy for the
instructional behaviour of every NEXA education agent. It is machine-readable
(contract `nexa.pedagogy/1.0`) and has 45 rules with ids.

**The runtime applies it; prompts do not repeat it.**

- An agent's manifest references the policy by version and profile, for
  example `"pedagogy": {"policy": "nexa-teaching-policy", "version": "1.0.0", "profile": "tutor"}`.
- On every call, the Agent Runtime adds the profile's rules for that audience
  and grade to the agent's own system prompt.
- The agent's prompt says only who it is and what its task is. Validation
  refuses an agent prompt that restates the policy (for example "step by
  step", "cite", "[S1]", "age appropriate", "Tok Pisin").
- Changing teaching behaviour means a new policy version, reviewed once,
  instead of edits to every agent's prompt.

Code:

| | |
|---|---|
| The policy | `v1/agent_runtime/pedagogy/policies/nexa-teaching-policy-1.0.0.json` |
| Release digests | `v1/agent_runtime/pedagogy/policies/released.json` |
| Contract, composition and screening | `v1/agent_runtime/pedagogy/contract.py` |
| Read API | `v1/agent_runtime/pedagogy/api.py` |
| Published schema | `v1/agent_runtime/schema/nexa-pedagogy-1.0.schema.json` |
| Manifest reference | `pedagogy` field of `nexa.agent/1.0` (optional field; old manifests keep their fingerprints) |
| Runtime | `runtime.py` (composition, escalation) |
| Engine | `orchestration/engine.py` (escalation before any step; grade and internal use passed per step) |
| Agent versions under the policy | `v1/agent_runtime/agents/proposed/` |
| Tests | `v1/tests/test_teaching_policy.py` (47 tests) |
| Live check | `v1/tools/teaching_policy_check.py` |
| CI | the *Teaching Intelligence Policy* step |

## The policy

| Topic (task) | Section | Rules | In short |
|---|---|---|---|
| Age/grade-aware explanation style | grade bands | TP-GRADE-01..06 | Three bands: **Grades 7–8**, **9–10**, **11–12**. Each sets vocabulary, how new terms are introduced, and how much reasoning is expected. If the grade is not given, the Grades 9–10 style is used. |
| Curriculum grounding | grounding | TP-GROUND-01..04 | Answer school-subject questions from the approved passages. A miss is said first, and any general answer is labelled. Use the syllabus's own terms and methods. Never invent a source, outcome code or quote. |
| Step-by-step teaching and hints | steps | TP-STEP-01..04 | Numbered steps, one idea each. Students get a hint ladder: a pointing question, then a hint, then the first step, then the full solution. Offer a practice question afterwards. Teachers get the full method and common errors. |
| Answer directly vs scaffold | answer_mode | TP-MODE-01..05 | Facts, definitions, concepts and checks of the student's own answer: answer directly. Homework-like problems, or "I'm stuck": scaffold. **Never complete assessed work.** The full solution on request after trying. Teachers get complete answers and answer keys. |
| Misconceptions | misconceptions | TP-MISC-01..03 | Say kindly what is wrong, explain why the wrong idea seemed reasonable, and give a contrasting example. Never agree with a wrong answer to be polite. |
| Uncertainty and abstention | uncertainty | TP-UNC-01..03 | Say when unsure and never guess numbers, dates, names or formulas. Ask one question when unclear. Decline outside school learning, in one sentence, and offer what NEXA can do. |
| Examples for PNG | context | TP-CTX-01..04 | Kina and toea, markets, gardens (kaukau, taro, sago, coffee, cocoa), fishing, PMVs, rivers and rainfall, rugby league. Examples must work in rural and urban schools. No stereotypes and no sides on politics, land or religion. Never use personal details. |
| Student vs teacher tone and permissions | roles | TP-ROLE-01..04 | Students: warm and encouraging, learner content only (no teacher guides or answer keys). Teachers: professional and concise; the teacher decides what is used. NEXA never claims to be a person or the Department of Education. |
| Human escalation | escalation | TP-ESC-01..04 | Distress, abuse or danger: stop and point to a person (TP-ESC-01, below). Marks, discipline, special needs, exam rules and individual students go to the teacher. No personal medical, legal or financial advice. A teacher's welfare concern goes to the school's child protection procedure. |
| Sources and citations | citations | TP-CITE-01..03 | Cite every curriculum statement with its marker. Only cite markers that were supplied. Calculations are attributed to the calculator. A teacher-facing suggestion is marked as a suggestion. |
| Language, including English/Tok Pisin | language | TP-LANG-01..03 | Clear English, the language of instruction and examinations, with subject terms in English. A student writing in Tok Pisin gets simple English that shows it understood them. No Tok Pisin replies until **LANG-D1** is decided. |
| (Output format) | format | TP-FMT-01..02 | Exact formats when asked (one word, JSON); never personal details. |

**Profiles** choose which sections apply:

| Profile | Used for | Sections |
|---|---|---|
| `tutor` | Teaching and subject questions | All |
| `concise` | Short exact answers | All except steps |
| `planning` | Lesson plans | All except answer mode |
| `assessment` | Assessment items | Grounding, citations, misconceptions, uncertainty, context, roles, escalation, language, format |
| `internal` | Engine-only calls (classification, coordinator choice, writing an expression) | Format only |

**The audience** comes from the verified roles: a student gets the
student-only rules and a teacher the teacher-only rules. **The grade** comes
from the workflow step (`"grade": "{{input.grade}}"`).

### Decision points (open, for governance)

| Id | Question | Meanwhile | Owner |
|---|---|---|---|
| **LANG-D1** | May NEXA reply in Tok Pisin when a student writes in Tok Pisin? Options: English only; English with Tok Pisin glosses; Tok Pisin explanations for Grades 7–8 with English subject terms. | English only (TP-LANG-02) | Mr. Chandana, with curriculum advisers. A Tok Pisin evaluation set and a fluent reviewer are needed before any other option. |
| LANG-D2 | Teacher materials for parents and communities in Tok Pisin or Hiri Motu? | No | Mr. Chandana |
| LANG-D3 | Subject terms always in English? | Yes (proposed) | Curriculum advisers |
| ESC-D1 | Confirm the helpline and TP-ESC-01 wording with the school counselling service, and the re-check interval. | 1-Tok Kaunselin Helpim Lain, 7150 8000, checked 24 Nov 2026 | Mr. Chandana |
| MODE-D1 | How strict is scaffolding for Grades 11–12 revision? | Scaffold first; full solution on request | Curriculum advisers |

## How it runs

```
agent call ──► manifest says pedagogy 1.0.0 / tutor
               ├─ screen the learner's message (ids only)
               │    distress or abuse ─► the policy's response with a person to talk to; no model call
               │    Tok Pisin          ─► note for TP-LANG-02
               ├─ compose: profile sections + audience rules + grade band   (internal profile for engine-only calls)
               └─ system = agent's own prompt + policy block ──► Model Gateway
                  recorded: policy, version, profile, audience, band, digest (x_nexa, trace, audit)
```

**Escalation is enforced in code, not left to the model.**

- A workflow screens its input before any step runs.
- A direct agent call is screened before the model is called.
- On a trigger (first-person self-harm, or disclosure of abuse), the run ends
  with the policy's response: talk today to a teacher, school counsellor or
  trusted adult; **1-Tok Kaunselin Helpim Lain, 7150 8000, free, 24 hours**;
  and, in danger, go somewhere safe.
- Only the trigger id is recorded, never the message.
- The triggers are first-person and specific. "What is suicide
  prevention?", "the cell kills itself" and "I am dying to know" do not
  trigger them (tested).
- The helpline number is listed in the policy as a public contact, so
  redaction and the personal-data checks leave it in place.

**Released versions are immutable.** `released.json` holds the sha256 of each
released file. Loading fails if a released file was changed, and CI and the
runtime both check this. A change is a new version:

```
python -m v1.agent_runtime.pedagogy release 1.1.0
```

**Everyone can read the policy.**

- `GET /v1/teaching-policy`: the versions, with their digests and the agents
  that use each one.
- `GET /v1/teaching-policy/1.0.0`: the whole policy.
- `GET /v1/teaching-policy/1.0.0/compose`: exactly what a call would add, for
  a given profile, audience and grade. AI engineers and admins only.

## Agents under the policy

New versions of all four education agents reference the policy. Their
prompts were cut to identity and task; the teaching rules they used to carry
now come from the policy.

| Agent | Current (serving) | Proposed | Profile | State |
|---|---|---|---|---|
| nexa-tutor | 1.0.0 | **1.1.0** | tutor | review |
| nexa-precise | 1.0.0 | **1.1.0** | concise | review |
| nexa-curriculum-tutor | 0.2.0 | **0.3.0** | tutor | review |
| nexa-lesson-planner | 0.1.0 (draft) | **0.2.0** | planning | draft |

**Governance:**

- The proposed versions are registered for **review**, and the current
  versions keep serving until each proposal is approved by someone other
  than its registrant (four eyes, M3-002) and activated.
- From now on, any new version of an education agent must reference a
  released policy. The registry refuses it otherwise, and activation checks
  again.
- Versions released before the policy are listed by name in
  `LEGACY_WITHOUT_PEDAGOGY`. The list is closed, and a test fails if it grows.

**Workflows** pass what the policy needs:

- `nexa-wf-assistant` 1.2.0 and `nexa-wf-curriculum-tutor` 1.2.0 mark their
  classifier and expression steps as internal.
- `nexa-wf-lesson-explainer` 1.2.0 passes `{{input.grade}}` to its agents.
- The coordinator's choice is always internal.

## Review against NEXA-GOV-001

**Provisional.** The NEXA-GOV-001 document is not in the repository. This
review checks the policy against the NEXA-GOV-001 controls that the
repository already applies and cites (M2-001, M2-002, M3-001, M3-007,
M3-008). It must be confirmed against the full text when that is provided
(the policy records `governance.review: "provisional"`).

| NEXA-GOV-001 control, as applied in NEXA | Where the policy implements it | Result |
|---|---|---|
| Curriculum answers only from approved, permitted sources, with citations (M2-001) | TP-GROUND-01..04, TP-CITE-01..02 | consistent |
| General answers labelled as not from the curriculum (M2, M3-001 safety) | TP-GROUND-02 | consistent |
| No personal data in prompts or outputs; learners include minors (M3-001, M3-007) | TP-CTX-04, TP-FMT-02; escalation records ids only; redaction kept (the helpline is a declared public contact) | consistent |
| Student and teacher access separated (learner content vs teacher-only, M2-001, M3-007) | TP-ROLE-02; the audience comes from verified roles, not the message | consistent |
| Human oversight of what reaches learners (M3-003 review checkpoints) | TP-ROLE-03, TP-ESC-02/04; the teacher decides | consistent |
| Safety refusals: harm, self-harm, adult content, cheating on assessment, political persuasion (`nexa.safety.student/1`) | TP-ESC-01 (enforced in code), TP-MODE-03, TP-CTX-03, TP-UNC-03 | consistent; TP-ESC-01 goes beyond refusal by pointing to a person |
| Transparency and traceability (M3-008) | Each call records policy, version, profile, audience, band and digest; versions immutable; read API | consistent |
| Change control and approval (M3-002) | Policy versions immutable with digests; agents under the policy need four-eyes approval | consistent |
| Inclusion and cultural appropriateness | TP-CTX-01..03, TP-LANG-01..03, LANG-D1..D3 | open decisions recorded |
| Retention of what is recorded (open item, M2/M3) | Records no message text for escalations | depends on the open retention decision |

## Testing

- **Tests:** 47 in `test_teaching_policy.py`. They cover:
  - coverage of all eleven topics, and immutability;
  - the schema, and the contract's own consistency rules;
  - student vs teacher rules, and the grade bands;
  - the internal profile, and the Tok Pisin note;
  - escalation triggers, positive and negative;
  - the manifest rules: reference required, no restating, released version
    and agent profile only, the closed legacy list, unchanged legacy
    fingerprints;
  - the registry: proposals in review, the current version keeps serving,
    activation;
  - the runtime: the policy appended to the agent's own prompt, the internal
    profile, legacy versions unchanged, distress answered without a model
    call;
  - workflows: escalation before any step, grade and internal use passed
    through, the M3-010 compatibility suite still passing with the proposals
    active;
  - the read API.
- **Full suite:** 530 passed (Postgres tests in CI).
- **On the DGX** (`teaching_policy_check.py`):
  - the served policy matches the release digest;
  - composition by audience and grade;
  - the proposals are registered under the policy;
  - distress is answered by the policy with no model call, for a student and
    for a teacher;
  - ordinary questions are unaffected;
  - once an approved version is active, real calls carry the policy.

## For approval

1. **Policy 1.0.0.** Approve, or ask for changes. The policy itself is a
   proposal until approved: approval is recorded as a new version with
   `status: approved` and `approved_by`.
2. **The proposed agent versions** (nexa-tutor 1.1.0, nexa-precise 1.1.0,
   nexa-curriculum-tutor 0.3.0). Approve and activate them through the
   registry, by someone other than the registrant.
3. **The decision points**, LANG-D1 first.
4. **NEXA-GOV-001.** Provide the document, so the review above can be
   confirmed.

## Not in this change

- **The education agents themselves** (tutoring, lesson planning,
  assessment): those are the next M4 tasks, and they run under this policy.
- **Evaluation of teaching quality against the policy** (an eval set per
  rule, including Tok Pisin): needed before approving LANG-D1 options. It
  belongs with the M4 evaluation task.
