# NEXA-M2-010: v0 Knowledge Migration and M2 Exit Review

Status: complete, submitted for approval (2026-11-17). Owner: Yasaru. Approver: Mr. Chandana.

## 1. Purpose

Bring the useful knowledge of NEXA v0 into the governed v1 Knowledge Service without carrying over what
v1 cannot trust, record a decision for every v0 asset, run every M2 check on the deployed system, and
submit the M2 technical exit evidence for approval before M3 (agents) begins.

## 2. What v0 holds (from the code, `main.py`, and NEXA-M0-004)

| Asset | Where | v0 handling | Class |
|---|---|---|---|
| Curriculum PDFs | top of the v0 folder (`glob <workspace>/*.pdf`) | PyPDFLoader, page-split, metadata guessed from the file name (subject, grade, term, doc type) | curriculum knowledge |
| EduNeX lesson notes (.docx) | repository root (41 files at M0) | python-docx | curriculum knowledge |
| Chroma store | `~/Nexa/chroma_store`, collection `curriculum_db`, cosine | 1,500-character chunks, 300 overlap, nomic-embed-text with task prefixes | derived index |
| LMS materials | inside Chroma only, via `/ingest-material` | text split and embedded directly; the file is not kept | curriculum knowledge, source outside v0 |
| `user_memory.json`, MySQL chat log, `uploads/` | v0 folder, MySQL | per-user facts, chats, learner attachments | **learner data** |
| `assets/` | v0 folder | answer PDFs and images v0 generated | generated output |

Hosts with a v0 installation: core NEXA (DGX, the repository folder) and the Pomnsoe school server
(`~/Nexa`, not a git checkout; its PDFs include Engineering and Technology documents that the DGX does not
have, NEXA-M2-009).

## 3. Migration rules

1. **The v0 index is never copied.** Its vectors come from v0 chunking (page-split, 1,500/300) and carry no
   source version, checksum, approval, scope or access classification, so v1 could neither cite nor govern
   them. Every source in it is reprocessed from its file, or re-synced from the LMS.
2. **Identity is the file's bytes.** A v0 file whose sha256 equals a v1 source's `content_checksum` is
   already in v1 (retained). Names are not trusted for identity.
3. **Metadata comes from v1's own rules** (`v1.knowledge.curriculum.draft_from_filename`), mapped to the
   M2-001 authority model: syllabus tier 1, teacher guide tier 2 (teacher-only), student guide tier 3,
   EduNeX lesson notes `lms_content` tier 4 with `lms.item_id` = the Moodle course module (`cm8879`).
4. **Nothing is ingested unapproved.** A reprocessed file enters v1 through the M2-002 pipeline with its
   approval recorded (`approved_by`); a file whose type or subject v1 cannot read is held for a curriculum
   officer to classify.
5. **LMS material held only inside Chroma is re-synced from the LMS**, its authoritative source
   (NEXA-M2-007). Text recovered from v0 chunks is only a last resort, for review, never indexed directly.
6. **Learner data is excluded**, never migrated, and handled as a security item (section 6).

Decisions per item: `retained`, `reprocess`, `resync`, `review`, `deprecated` (duplicates, the v0 index),
`excluded` (learner data, generated output). Tool: `v1/tools/v0_inventory.py` (read-only), which records the
decision and the reason for every file and every document inside the Chroma store.

## 4. Inventory results

### Core NEXA (DGX), 2026-11-17, v0 folder `/home/admin/Nexa/Nexa`, v0 index `~/Nexa/chroma_store`

| What | Count | Decision |
|---|---|---|
| v0 curriculum files (PDF, .docx) | 70 | **retained**: every one is byte-identical (sha256) to a governed v1 source |
| `user_memory.json` | 1 | **excluded**: learner data (section 6) |
| v0 vector index `curriculum_db` | 9,774 chunks, 556 documents, dimension 768 | **deprecated**: not copied (section 3, rule 1) |
| Documents that exist only inside the v0 index (LMS uploads through `/ingest-material`) | 527 | matched against v1 by text: those v1 already holds through the LMS sync are **retained**, the rest **resync** from the LMS (result: section 4.1) |

### Pomnsoe school server, 2026-11-17, v0 folder `/home/nvidia/Nexa`

v0 here keeps no index on disk: `main.py` names the collection (`curriculum_db`) but no persistence folder,
and no Chroma or FAISS file exists on the server. It rebuilds the index in memory from its PDFs at every
start, so its knowledge is exactly its 15 PDFs; anything uploaded to it through `/ingest-material` lived
only in memory until the next restart.

| File | Decision | Note |
|---|---|---|
| STEM-CHEMISTRY-SRB.pdf | retained | same bytes as v1 `png-ndoe:stem-chemistry-student-guide-gr12-t1` |
| STEM-CHEMISTRY-Syllabus.pdf | retained | same bytes as v1 `png-ndoe:stem-chemistry-syllabus-gr12-t1` |
| STEMBIOLOGYTG.pdf | retained | same bytes as v1 `png-ndoe:stem-biology-teacher-guide-gr12-t1` |
| STEMBIOLOGY-Syllabus.pdf | retained, **label conflict** | same bytes as v1 `png-ndoe:stem-biology-student-guide-gr12-t1`: v0 calls the file a syllabus, v1 a student guide; a syllabus outranks a guide, so a curriculum officer must confirm which it is |
| STEM-ENGINEERING-Syllabus, -TG, -TRB, STEM-NGINEERING-SRB (sic) | review | Engineering is not in v1 yet; grade and term are not in the names |
| STEM-TECHNOLOGY-Syllabus, -TG, -TRB, -SRB | review | Technology is not in v1 yet; grade and term are not in the names |
| STEM-CHEMISTRY-TRB, STEMBIOLOGY-SRB, STEMBIOLOGY-TRB | review | not in v1; grade and term are not in the names |
| `assets/` (3 images, 4 lesson PDFs) | excluded | generated by v0 |
| `user_memory.json` | excluded | learner data |

The 11 files under review are new knowledge for v1 (Engineering and Technology above all). They enter v1
through the M2-002 pipeline once a curriculum officer records their grade, term, type and approval.

### 4.1 Final decisions, DGX (2026-11-17, `7be8a3a`)

Each of the 527 documents that existed only inside the v0 index was asked of v1 with its own text and
compared word for word (`v0_inventory.py`, rules in section 3).

| Decision | Items | What |
|---|---|---|
| retained | 521 | 70 curriculum files byte-identical to v1 sources; 451 v0-only documents v1 holds with the same text (60%+ of the words), from the LMS sync |
| deprecated | 57 | 52 superseded (an older revision or another format of a lesson v1 holds: same title, 30-60% of the words; v1's copy is the current one); 4 test uploads (`Test_...`); the v0 vector index (9,774 chunks) |
| excluded | 3 | `user_memory.json` (learner data); 2 files v0 generated and that were uploaded back (`lesson_20260724_092800.pdf`, `edunex_quiz_course11_20260725_095306.pdf`) |
| resync | 18 | below |

The 18 left for the LMS:

| Document (v0 name) | v0 course label | Action |
|---|---|---|
| 11.2 Nutrition - Autotrophs.pdf; Activity.pdf; Biology 2012.pdf; In-class Assignment.pdf; Mendelian Genetics notes.pdf; Photosynthesis - Copy.pdf; B1RevisionPowerPointpptx.pdf | Biology (Grade 11 / Advanced) | re-send from the LMS if still published and approved; not in v1 |
| Basics of Climate Change.pdf; ELEMENTS OF WEATHER AND THEIR INSTRUMENT.pdf; Ocean Currents.pdf | Geography - Grade 11 | Geography is outside the current STEM scope: decide whether NEXA covers it, then re-send |
| 2D_Motion_Lecture_Note V1.docx; Topic_2_PPT_Dimensional_Analysis v1 (1).pptx; Properties_and_Structure_of_Matter.pptx | filed under Introduction To Java / General Mathematics in v0 (content is Physics and Chemistry) | re-send under the right course; not in v1 (closest match 1-2%) |
| 12.3_Biotechnology_4_DNA Sequence Analysis (BLAST, CLUSTAL).pptx; 12.5_Battery Technology_2_Paper Batteries.pptx | Biology | probably held: 54-56% of the words match a v1 item titled only "PPT"; the LMS team confirms, nothing is assumed |
| JavaScriptTutriol_ Variable and DataType.pdf; quiz_python.pdf; POMNSOE_LMS_Backup_Guide_English (1).docx | various | not curriculum: do not re-send |

v0's course labels are unreliable (Chemistry and Physics material filed under General Mathematics, Accounting,
Business Studies and Introduction To Java), which is one more reason the v0 index is not copied (rule 1).

**Result.** No v0 curriculum knowledge is lost: every v0 source is in v1, superseded by a v1 copy, or
listed above for the LMS team (15 items, 3 of them pending a scope decision) or for a curriculum officer
(the 11 Pomnsoe files).

## 5. M2 exit criteria

| Criterion | Evidence | Status |
|---|---|---|
| Knowledge Service integrated with NEXA Core | Exit suite 2026-11-17 (`ee10507`, deployed `4e2bce2`), step 1: five v1 services healthy, access control, grounded answers; 19 of 20 checks passed, the 20th was an outdated expectation (below) | met; check updated |
| Approved curriculum corpus is versioned and traceable | M2-001/M2-002 registry; step 5: the original file of all 60 cited source versions is stored and hashes to its recorded checksum; step 6: all 70 v0 curriculum files are byte-identical v1 sources | met |
| Retrieval benchmark and grounding thresholds pass | Steps 3 and 4: corpus suite Gate PASS (with v0 baseline); live suite Gate PASS as admin, as a Pomnsoe student, and with course context | met |
| Citation provenance is verified | Step 5: PASS, 109 citations used in 47 of 48 answers, every one traced to an active, approved chunk, its source version and the original file by sha256 | met |
| EduNeX/LMS synchronisation is demonstrated | Step 2: M2-007 demonstration PASS on the DGX; step 1: 474 LMS items active, 5 withdrawn, none waiting or failing | met |
| Edge/offline knowledge design validated | NEXA-M2-009, complete: offline live suite PASS on the Pomnsoe school server | met (2026-11-16) |
| Security/data controls reviewed | Section 6: controls in place and verified; three open items (learner data in the repository, branch protection, NEXA-GOV-001) | reviewed, open items tracked |
| Exit evidence submitted for approval | This document, the exit suite summary (`~/nexa-evidence/m2-exit-20261117-100110/SUMMARY.md` on the DGX) and the M2 exit evidence document | submitted by Yasaru |

## 6. Security and data controls review

| Control | Where | State |
|---|---|---|
| Authentication on every knowledge endpoint (signed identity tokens) | `v1/identity`, `require_principal` | in place, tested |
| Role and access-classification filtering (learner / teacher-only / restricted), school and LMS-site scope | `retrieval_filter`, M2-001; live check step 1, student-view benchmark | in place, verified on the DGX |
| School packages hold only national and own-school approved content | NEXA-M2-009, `test_package_holds_only_what_this_school_may_have` | in place, verified on the Jetson (4,576 chunks of another school excluded) |
| Secrets only in root-only `/etc/nexa/*.env`, never in the repository | NEXA-M1-006; scan of the v0 root files on 2026-11-17: no hard-coded secrets | in place |
| Audit events for refusals and access decisions | `audit_event` in the Knowledge Service | in place |
| Dependency vulnerability audit in CI | `pip-audit` gate, with a time-boxed Pillow exception until 2027-01-31 | in place |
| **Learner data in the repository** | `user_memory.json` at the repository root still holds 5 entries with email addresses, in the working tree and in git history (committed 2026-07-25); first raised in NEXA-M0-004 | **open, high**: remove from the working tree now; purging git history needs the repository owner (a history rewrite and force-push) |
| Release protection | `main` has no branch protection, so a failing check does not block a merge | **open**: require `build-test` on `main` |
| Data handling against NEXA-GOV-001 | document not yet available to the developer | **open**: confirm when provided |

## 7. Known limitations and follow-up backlog

- Curriculum officer: classify and approve the 11 Pomnsoe v0 files (Engineering and Technology above all)
  for ingestion; confirm whether `STEMBIOLOGY-Syllabus.pdf` (v1: Grade 12 Term 1 Biology Student Guide) is a
  syllabus or a guide.
- LMS team: re-send the 15 documents of section 4.1 (after the Geography scope decision); confirm the two
  matched only to v1 items titled "PPT".
- Remove `user_memory.json` (5 email addresses) from the repository and purge it from git history
  (repository owner); retire v0 on the DGX and the Pomnsoe school server, where it also writes learner data.

- Exit suite step 1 expected an off-curriculum question to get the strict "not found" reply, written before
  general answers (NEXA_GENERAL_ANSWERS) existed. It now gets a labelled general answer with no curriculum
  citation, the intended behaviour; the check is updated to test that. The answer also shows a limit: the
  local model's knowledge ends in October 2023, so for current events it can only say it may be out of
  date (web mode answers these).

- Moodle plugin: send course grade and subject (`context`) with each question (M2-008 follow-up).
- Before EduNeX LMS content is made visible to all schools: link each Moodle lesson to its curriculum copy
  (`lms.item_id` = `cm…`), so one lesson is one source.
- Limits for the two new hallucination measures, from the next DGX baseline.
- A second developer reproduces the corpus suite with real models.
- QA host, CI deploy jobs on `main`, live suite after each deploy.
- Core NEXA takes the answer-writing overflow from school servers (needs the school-to-DGX link).

## 8. M2 exit recommendation

All M2 exit criteria are met with recorded evidence, except that the security review has three open
items that do not block M3 agent work but must be closed by named owners: learner data in the
repository (repository owner), branch protection on `main` (repository admin) and the NEXA-GOV-001
confirmation (governance). Recommendation: approve the M2 exit and begin M3, with those three items
and the backlog in section 7 tracked to closure.

## 9. How the evidence is produced

On the Pomnsoe school server (read-only), then on the DGX:

```bash
# school server: inventory of its v0 folder (no v1 comparison possible there)
/opt/nexa-edge/venv/bin/python /opt/nexa-edge/app/v1/tools/v0_inventory.py --v0-dir ~/Nexa --out ~/nexa-evidence
# DGX: the whole exit suite, then the school server's inventory against the DGX's v1
bash ~/nexa-build/v1/deploy/m2_exit_suite.sh
python v1/tools/v0_inventory.py --from-report <copied school inventory .json> --v1-url http://127.0.0.1:8103 --out ...
```
