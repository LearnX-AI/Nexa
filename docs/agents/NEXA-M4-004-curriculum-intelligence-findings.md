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

Open questions, checked by rerunning as an administrator and with grade and subject given:

1. Is the Physics "no evidence" an access-scope effect (school-scoped content not visible to this teacher) or a retrieval or evidence-threshold effect?
2. Do grade and subject, which `curriculum_map` would supply, fix the cross-grade and cross-subject citations?

## Next

1. Post the findings in ClickUp.
2. Run the demo with the digitization team and CDD reviewers, and collect the sign-off sheets.
3. Mr. Chandana approves the amendments.
4. Implementation, evaluated offline in CI and live on the DGX against agreed thresholds.
