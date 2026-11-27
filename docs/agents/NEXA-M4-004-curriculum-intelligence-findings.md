# NEXA-M4-004: Curriculum Intelligence, findings before implementation

Status: **findings and demo pack**. No Curriculum Intelligence change has
been made; implementation starts after the findings are approved. Owner:
Yasaru.

**Scope agreed on 27 Nov 2026:** Grades 7–10 are not available. M4-004
covers the Grade 11–12 Biology, Chemistry and Physics curriculum that exists.
Every other grade and subject gets an explicit "not covered by the curriculum
yet", never an answer from another grade or subject.

## Evidence (DGX, 27 Nov 2026)

Reports are on the DGX under `~/nexa-evidence/m4-004/`.

| Question | Finding |
|---|---|
| S3 access | A read-only IAM user, `nexa-curriculum-readonly`, can list and read `s3://learnxplus-backups/POM-NSOE Synchronization/` only. The rest of the bucket and other buckets are denied (verified). The keys are in `/etc/nexa/s3-curriculum.env` on the DGX only (root, 600). The bucket policy was not changed. Bucket versioning is off. |
| What S3 holds | The six POM-NSOE EduNeX courses, Grade 11–12 Biology, Chemistry and Physics: 855 files, 5.9 GB, last changed 26 Nov 2026. There are no official syllabi or guides and no other grades or subjects. The six `.mbz` Moodle backups (2.8 GB) are excluded, because they may contain learner data. |
| End-to-end status | A 31-file pilot from all six courses was checked against the live index by content: 24 were already indexed through the EduNeX sync. The new content is a few HTML worksheets and quizzes and probably one PPT. NEXA's extractors read all 31 files (no OCR needed). Only 4 of the 31 files contain outcome codes in the text. |
| Indexed today | 497 active sources, Grades 11–12 only: 31 official documents (12 syllabi, 19 teacher and student guides) and 466 LMS items. |
| M2-008 benchmark | Reusable. Live rerun PASS 7/7: recall@5 83.3 (gate ≥ 80), accuracy 0.741, 0 fabricated citations, all unanswerable questions refused end to end. Weak spots: Biology recall@5 69.0, LMS content 69.6, citation precision 0.53. The same benchmark scores 95.8 on a clean index, so the LMS content dilutes retrieval. |

## Gaps for M4-004

1. The official curriculum for other grades and subjects is not available to NEXA.
2. Outcome and progression structure: outcomes, prerequisites, related outcomes. Documents rarely carry outcome codes.
3. Mapping and alignment against the official syllabus, with confidence and sources. Retrieval confidence alone is not enough: 9 of 16 unanswerable questions still retrieve evidence.
4. Source and version accuracy (citation precision 0.53).
5. Retrieval dilution by duplicated LMS content, and variance on identical queries.

## Suggested minimal amendments (existing components only)

1. An S3 source adapter in the M2-002 pipeline: read-only, de-duplicated, with change detection by ETag. It is used only for material NEXA does not have.
2. A curriculum outcome registry built from the official syllabi already indexed, validated by CDD before use.
3. One governed Tool Gateway tool, `curriculum_map`: grade, subject, unit, outcome, confidence and sources. Where the syllabus does not support a mapping, the tool says so.
4. M2-008 extended with CDD-labelled mapping, alignment and source/version items, and a citation-precision gate.
5. Retrieval de-duplication and authority weighting, released only through the M2-008 gate.

There are no new services, no model-weight changes and no automatic
publication. The model gateway stays hardware-independent.

## Demo pack

`v1/tools/curriculum_demo.py` (tests: `v1/tests/test_curriculum_demo.py`)
is read-only. It asks the live Knowledge Service 28 questions as a teacher
at the pilot school:

| Category | Cases | Expected |
|---|---|---|
| Covered | 12: two per course | A grounded answer citing the right subject and grade, with the key terms |
| Not covered | 6: English G7, Social Science G8, Mathematics G9, Science G10, Business G11, History G12 | No curriculum answer; an answer from another grade or subject is reported as a silent substitution |
| No answer | 5 | Refusal |
| Incorrect premise | 5 | The answer corrects the premise from the curriculum; a refusal does not pass |
| Source and version | every citation | An active source version with a content checksum |

Covered and incorrect-premise questions are asked twice:

- as typed;
- with their grade and subject as retrieval filters (ids ending in `f`), which is what an agent that knows the learner's grade and subject would get.

Grounded answers are classed by their sources: right course, another grade of the same subject, or another subject.

The report includes:

- the inventory;
- the S3 pilot table with ETags;
- a **sign-off sheet**: CDD reviewers mark each answer correct, partly correct, incorrect or a correct refusal, and the digitization team signs off source integrity.

```bash
cd /home/admin/Nexa/Nexa
(set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \
 /home/admin/Nexa/venv310/bin/python -m v1.tools.curriculum_demo --out ~/nexa-evidence/m4-004/demo)
```

## First demo run (DGX, 27 Nov 2026, as a teacher at POMNSOE)

| | Result |
|---|---|
| Covered | 3/12. Biology and Chemistry: 8 grounded, but 5 of them cite another grade (Grade 12 for Grade 11 questions) or another subject (DNA structure from the Physics student guide; reaction rates from Biology notes). Physics: all 4 got no evidence, although the matching Grade 11–12 Physics lesson notes are indexed. |
| Not covered (Grades 7–10, other subjects) | 6/6 refused, with no silent substitution |
| Cannot be answered | 5/5 refused |
| Incorrect premise | All 5 refused (no evidence). The first version counted that as a pass; a refusal no longer passes, because these topics are covered and the premise should be corrected. |
| Sources | 0 fabricated citations; 23 cited, all active versions with a checksum, 9 official |

### Cause 1: POMNSOE cannot see its own courses

The EduNeX content (466 items, the same courses as the S3 folder) was synced
under a test school, `edunex-core-test`. School-scoped content is visible
only to users of that school, so POMNSOE users see only the national
sources: the official syllabi and guides, and the M2 corpus's Grade 12
lesson notes. This applies to the Student Tutor and the Teacher Assistant
too. The M2-008 live benchmark runs as platform admin, which sees everything,
so its figures overstate what POMNSOE learners get.

Checked on the DGX: the lesson notes for Quantities & Measurement, Waves,
Classical Mendelian Genetics and Amino Acids are all `scope=school`,
`school=edunex-core-test`.

### Demo as a POMNSOE teacher and with the EduNeX content visible (27 Nov 2026, `88ef0a1`)

| | POMNSOE teacher today | Teacher at `edunex-core-test` (content visible) |
|---|---|---|
| Covered | 4/12 | 10/12 |
| Covered, grade and subject given | 6/12 | **12/12** |
| Physics | 0/4 | 4/4 |
| Grounded answers without grade or subject: right course / another grade / another subject | 4 / 3 / 1 | 10 / 2 / 0 |
| Not covered (Grades 7–10, other subjects) | 6/6 | 5/6: **a Grade 7 English question was answered from Grade 11 English material** (silent substitution) |
| Cannot be answered | 5/5 | 5/5 |
| Incorrect premise | 0/5 (refused) | 0/5 (refused) |
| Fabricated citations | 0 | 0 |
| Cited sources that are official syllabi or guides | 19 of 37 | **2 of 79** |
| p95 per answer | 7.9 s | 8.6 s |

Reports: `~/nexa-evidence/m4-004/demo-pomnsoe/` and `~/nexa-evidence/m4-004/demo-edunex/`.

### Cause 2: false premises are refused, not corrected

For "Why is the mitochondrion the site of photosynthesis?" and "Why does a
strong acid have a pH greater than 7?":

- retrieval finds strong evidence (similarity 0.78, 5 passages, the right material);
- the answer model then decides that "the passages do not answer the question", and NEXA refuses;
- the neutral questions ("Where does photosynthesis take place?", "What is the pH of a strong acid?") are grounded correctly.

Refusing is safe, because the false claim is never confirmed. But a learner
should be told the correct fact. The fix belongs in the grounded-answer step:

- where the evidence is strong and the passages contradict an assumption in the question, NEXA corrects the assumption with citations;
- it ships through the M2-008 gate, with premise items added, so that unanswerable questions are not answered with confident "corrections".

### What the evidence supports

1. **Re-scope the EduNeX courses to POMNSOE** (a re-sync with a POMNSOE token) after CDD signs off the content. It makes them visible to POMNSOE learners, so it is a publication decision, not a code change. Expected effect: covered questions from 4/12 to 10/12.
2. **`curriculum_map`: grade and subject mapping** (10/12 to 12/12). It must never substitute another grade silently: a grade NEXA does not hold gets "not covered", or the answer states which grade the material is from.
3. **Official-syllabus weighting** for outcome mapping and alignment (rule R3), because LMS content crowds out the syllabus (2 of 79 citations).
4. **Premise correction** in the grounded-answer step, behind the M2-008 gate.
5. **Measure as POMNSOE users:** M2-008 is also run as a POMNSOE learner, and the demo pack becomes part of the M4-004 evaluation.

## Next

1. Post the findings in ClickUp.
2. Run the demo with the digitization team and CDD reviewers, and collect the sign-off sheets.
3. Mr. Chandana approves the amendments.
4. Implementation, evaluated offline in CI and live on the DGX against agreed thresholds.
