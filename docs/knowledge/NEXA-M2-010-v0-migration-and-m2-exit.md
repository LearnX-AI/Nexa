# NEXA-M2-010: v0 Knowledge Migration and M2 Exit Review

Status: in progress (2026-11-17). Owner: Yasaru. Approver: Mr. Chandana.

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

To be filled from the DGX and Pomnsoe inventory runs (section 8).

## 5. M2 exit criteria

| Criterion | Evidence | Status |
|---|---|---|
| Knowledge Service integrated with NEXA Core | Five v1 services healthy, gateway routes and identity tokens (exit suite step 1); NEXA-M1-010; M2-006 answer API | to be confirmed by the run |
| Approved curriculum corpus is versioned and traceable | M2-001 metadata standard, M2-002 registry (source and version ids, checksums, runs); v0 inventory (step 6) | to be confirmed by the run |
| Retrieval benchmark and grounding thresholds pass | M2-008 corpus suite and live suite (steps 3, 4) | to be confirmed by the run |
| Citation provenance is verified | Provenance check, citation to chunk to original file by sha256 (step 5) | to be confirmed by the run |
| EduNeX/LMS synchronisation is demonstrated | M2-007 demonstration (step 2), live sync state (step 1) | to be confirmed by the run |
| Edge/offline knowledge design validated | NEXA-M2-009, complete: offline live suite PASS on the Pomnsoe school server | met (2026-11-16) |
| Security/data controls reviewed | Section 6 | review below |
| Exit evidence submitted for approval | This document and the exit suite summary | after the runs |

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

To be completed with the run results. Carried so far:

- Moodle plugin: send course grade and subject (`context`) with each question (M2-008 follow-up).
- Before EduNeX LMS content is made visible to all schools: link each Moodle lesson to its curriculum copy
  (`lms.item_id` = `cm…`), so one lesson is one source.
- Limits for the two new hallucination measures, from the next DGX baseline.
- A second developer reproduces the corpus suite with real models.
- QA host, CI deploy jobs on `main`, live suite after each deploy.
- Core NEXA takes the answer-writing overflow from school servers (needs the school-to-DGX link).
- Retire v0 on the Pomnsoe school server after its knowledge is migrated.

## 8. How the evidence is produced

On the Pomnsoe school server (read-only), then on the DGX:

```bash
# school server: inventory of its v0 folder (no v1 comparison possible there)
/opt/nexa-edge/venv/bin/python /opt/nexa-edge/app/v1/tools/v0_inventory.py --v0-dir ~/Nexa --out ~/nexa-evidence
# DGX: the whole exit suite, then the school server's inventory against the DGX's v1
bash ~/nexa-build/v1/deploy/m2_exit_suite.sh
python v1/tools/v0_inventory.py --from-report <copied school inventory .json> --v1-url http://127.0.0.1:8103 --out ...
```
