# NEXA-M2-001: Curriculum Source and Metadata Standard

Status: Draft for governance acceptance
Milestone: M2 (Knowledge, RAG and Curriculum Intelligence)
Schema version: 1.1.0 (1.1.0 adds optional LMS reference fields for NEXA-M2-007; 1.0.0 records stay valid)
Aligns with: NEXA-ARCH-001, NEXA-ADR-0002, NEXA-ADR-0004, NEXA-GOV-001, NEXA-MIG-001
Implementation: `v1/knowledge/metadata.py`, `v1/knowledge/authority.py`, `v1/knowledge/access.py`,
`v1/knowledge/curriculum.py`
Machine-readable schema: `v1/knowledge/schemas/source-metadata.v1.1.0.json` (previous: `source-metadata.v1.0.0.json`)
Validated samples: `v1/knowledge/samples/source_records.sample.json`

## 1. Purpose

One contract for every knowledge source in NEXA v1. The same record is written
by ingestion, copied onto every chunk, filtered and weighted by retrieval,
rendered into citations by provenance, and used to group evaluation results.
Nothing downstream re-derives source facts from file names or guesses.

## 2. Versioning policy

- `schema_version` is semver and carried on every record.
- Adding an optional field is a minor bump (1.1.0). Records at 1.0.0 stay valid.
- Removing a field, renaming it, or changing its meaning is a major bump (2.0.0)
  and requires a migration of stored records under NEXA-MIG-001.
- The JSON Schema is regenerated from the Pydantic model on every change and
  published at `v1/knowledge/schemas/source-metadata.<version>.json` and at
  `GET /v1/knowledge/schema/source-metadata`.

## 3. Identity and versions

| Concept | Rule | Example |
|---|---|---|
| `source_id` | Stable across versions. Lower case `[a-z0-9._:-]`, 3 to 128 chars, prefixed by origin. | `png-ndoe:stem-chemistry-syllabus-gr12-t1`, `edunex:cm8879`, `school:demo-high:teacher-deck-transcription` |
| `content_checksum` | `sha256:` of the original file bytes. Identifies one exact version. | `sha256:a30d64e8...` |
| `version_id` | Derived: `source_id@` + first 16 hex of the checksum. | `png-ndoe:stem-chemistry-syllabus-gr12-t1@a30d64e843d6737b` |

The same bytes are registered once, whatever name they arrive under. A new
checksum for an existing `source_id` is a new version (see M2-002).

## 4. Fields

Required fields are marked R. Fields set by the pipeline are marked P and are
never taken from a caller.

| Field | | Type | Meaning and rules |
|---|---|---|---|
| `schema_version` | R | `"1.0.0"` or `"1.1.0"` | Contract version. |
| `source_id` | R | string | See section 3. |
| `content_checksum` | R, P | string | See section 3. |
| `version_label` | | string | Edition or version as printed (`2023`, `Edition 2`). |
| `supersedes_checksum` | | string | Explicit predecessor, when known. |
| `source_type` | R | enum | Section 5.1. |
| `authority_tier` | P | 1 to 6 | Derived from `source_type`, never chosen freely. A caller value that disagrees is rejected. |
| `title` | R | string 3 to 300 | Human title used in citations. |
| `subject` | R | enum | Section 5.5. |
| `grade_levels` | R | list of 1 to 13 | Sorted, de-duplicated. |
| `term` | | 1 to 4 | School term. |
| `curriculum` | | object | `thema`, `strand`, `module_code`, `module_title`, `topic_code`, `topic_title`, `outcome_codes`. Chunks refine this per chunk. Outcome codes must match `NN.N[.N[a]]`. |
| `language` | R | BCP-47 | `en`, `tpi` (Tok Pisin), `ho` (Hiri Motu). Default `en`. |
| `scope` | R | enum | `national`, `provincial`, `school`, `class`. |
| `jurisdiction` | | object | `country` (default `PG`), `province`, `school_id`, `class_id`. |
| `publisher` | R for official types | string | e.g. `PNG National Department of Education`. |
| `approval_status` | R | enum | Section 5.3. Defaults to `pending_review` at ingestion. |
| `approved_by` | R when an official source is approved | string | Who approved it. |
| `access_classification` | R | enum | Section 5.4. |
| `effective_from` | R | date | First date the source may answer. |
| `effective_to` | | date | Last date. Must not be before `effective_from`. |
| `ingested_at` | P | datetime | Set by ingestion. |
| `source_uri` | R | URI | Scheme `repo`, `edunex`, `lms`, `s3`, `blob`, `file` or `https`. Must be percent-encoded (no spaces) and must not carry credentials or signed query parameters. |
| `file_name`, `mime_type`, `file_size_bytes`, `page_count` | P | | From the upload. |
| `lms` | R for `lms_content` | object | `system` (`edunex`, `lms`, `moodle`, `learnx`), `course_id`, `item_id`, `revision`; since 1.1.0 optional `site_id`, `kind`, `url`, `owner_ref` (opaque, never a name). |
| `external` | R for `external_reference` | object | `domain` (must be on the permitted list), `retrieved_at`, `license`. |
| `tags` | | list | Free tags. |

Unknown fields are rejected, so a typo cannot silently create a new field.

### 4.1 Location inside a source (Locator)

Every page, block and chunk carries a `Locator`, which is what a citation points at.

| Field | Meaning |
|---|---|
| `page_start`, `page_end` | 1-based physical page, or slide number. |
| `page_label` | The page number printed on the page (`VIII`, `19`). PNG curriculum PDFs number front matter in roman numerals, so physical page 10 is printed VIII. |
| `page_basis` | `physical` (PDF), `rendered_estimate` (DOCX, from Word's last rendered layout), `slide` (PPTX), `none` (HTML/LMS). Citations mark estimates as approximate. |
| `section_path` | Heading path, e.g. `Thema: Battery Technology > Module 12.1: Inductive Charging`. |
| `anchor` | Stable in-document anchor (`p12` paragraph, `t3` table, `s4.7` slide shape). |

## 5. Controlled vocabularies

### 5.1 Source types and authority tiers

| Tier | `source_type` | What it is |
|---|---|---|
| 1 | `official_syllabus` | NDoE syllabus: outcomes, content standards, performance indicators. |
| 2 | `teacher_guide`, `assessment` | Official teacher guides and assessment material. |
| 3 | `student_guide`, `lesson_notes` | Official student material (SRB, lesson notes published by NDoE). |
| 4 | `lms_content` | Content approved for EduNeX or another LMS. |
| 5 | `teacher_material` | School or class material created by teachers. |
| 6 | `external_reference` | Permitted external sources (allowlist below). |

Official types (tiers 1 to 3) must be `scope: national` and name a `publisher`.

Permitted external domains (NEXA-GOV-001, change by governance decision only):
`en.wikipedia.org`, `scholar.google.com`, `openstax.org`, `phet.colorado.edu`, `www.khanacademy.org`.

### 5.2 Scope

`national` > `provincial` > `school` > `class` in breadth. School and class scope
need `jurisdiction.school_id` (and `class_id`); provincial needs `province`.

### 5.3 Approval status

| Status | Ingested | Answers questions |
|---|---|---|
| `approved` | yes | yes, inside the effective window |
| `pending_review` | yes, chunked for review | no |
| `draft` | yes | no |
| `superseded` | kept for audit | no |
| `withdrawn` | kept for audit | no |

### 5.4 Access classification and who can retrieve it

| Class | Meaning | Roles that can retrieve |
|---|---|---|
| `public` | Anyone. | all |
| `learner` | Any authenticated learner or staff member. | all authenticated roles |
| `teacher_only` | Staff only: teacher guides, answer keys. | teacher, school_admin, ai_engineer, auditor, national_admin, admin, platform_admin |
| `restricted` | Named roles only: assessments before release. | national_admin, admin, platform_admin |

Service tokens get learner-level access only, because an agent may be acting
for a student. Passing the end user's roles through to Knowledge is an M3 agent
concern. School and class scoped sources are excluded from retrieval for
non-administrators until identity issues a school claim (open item 8.5).

Defaults applied from the source type: syllabus `learner`, teacher guide
`teacher_only`, student guide `learner`, assessment `restricted`, LMS content
`learner`, teacher material `teacher_only`.

### 5.5 Subjects

`Biology`, `Chemistry`, `Physics`, `Mathematics`, `English`, `Engineering`,
`Technology`, `General Science`, `Social Science`, `General`.

## 6. Authority and conflict rules

Applied in order by `authority.resolve()`; deterministic, ties end on `source_id`.

| Rule | |
|---|---|
| R1 Eligibility | Only `approved` sources inside their effective window are eligible. |
| R2 Version | For one `source_id`, the latest `effective_from` wins; older versions are treated as superseded. |
| R3 Claim class | `curriculum_outcome` (what students must learn, outcome codes, content standards, assessment criteria): only tier 1 is authoritative. Lower tiers may restate an outcome but never override it. `instructional` (explanations, examples, activities): ordered by tier. `local_context` (timetables, school practice): most specific scope first, then tier. |
| R4 Tier | Lower tier number wins. |
| R5 Scope | For outcome and instructional claims, national outranks local within a tier. Local material supplements; it does not override. |
| R6 Recency | Same tier and scope: newer `effective_from` wins. |
| R7 External last resort | Tier 6 is used only when no tier 1 to 5 source covers the question, and must be labelled external. |

Retrieval weights per tier (consumed by the retrieval and reranking step):
1.00, 0.95, 0.90, 0.85, 0.75, 0.60.

Evidence for R3 from the M2-003 benchmark: applying these weights to every
query lifts syllabus-outcome questions from 80% to 100% recall@5, but lowers
lesson-note table questions from 71% to 64%. Blanket multipliers penalise
approved LMS content for explanatory questions it is the best source for. The
retrieval step should therefore apply tier weighting by claim class (R3), not
uniformly.

### 6.1 Change from v0

v0 ranked LMS material above the syllabus (`DOCTYPE_PRIORITY`, main.py:1395:
LMS Material 5, Syllabus 3, Teacher Guide 2, Student Guide 1). This standard
puts the official syllabus first and approved LMS content fourth, as the M2
scope requires. This is an intended behaviour change and is recorded here for
NEXA-MIG-001.

## 7. PNG curriculum recognisers

`curriculum.draft_from_filename()` drafts metadata from the real naming
conventions. It ports v0's parsers (NEXA-MIG-001 reuse). The ingestion manifest
always wins over anything inferred.

| Pattern | Example | Drafted |
|---|---|---|
| `STEM<Subject>-Term<n>-<Doctype>-Gr<nn>` | `STEMChem-Term1-Syllubus-Gr12.pdf` | official syllabus, Chemistry, Grade 12, Term 1, NDoE, `png-ndoe:stem-chemistry-syllabus-gr12-t1`. The `Syllubus` spelling on two real files is accepted. |
| `sNN_cmNNNN_<module>_<Module title>_<n>_<Topic>` | `s04_cm8879_12.2_Genetics and Molecular Biology_3_DNA Replication.docx` | LMS content `edunex:cm8879`, Biology, Grade 12, module 12.2, topic DNA Replication. The `cmNNNN` number is treated as the EduNeX content-module id. |

In-text recognisers: `THEMA <TITLE>`, `MODULE <code>: <TITLE>`, `Topic <n>: <title>`,
activities and exercises, and outcome codes such as `12.1.1a`. Real headings in
the corpus are upper case. Mixed-case `Thema` and `Module 12.1` lines are table
cells and cross-references and are deliberately not headings. Headings wrapped
across lines or pages are rejoined. Thema values are canonicalised (trailing
acronyms dropped from the field, `Biotehcnology` mapped to `Biotechnology`) so
they filter cleanly; the heading text keeps the original wording.

## 8. Sample records and validation

Ten records from the real corpus are committed in
`v1/knowledge/samples/source_records.sample.json` and validated by
`test_committed_sample_records_validate`. They cover every source type the
corpus has: two syllabi, two teacher guides, two student guides, two EduNeX
lesson notes, the scanned syllabus extract, and a school teacher deck.

All 72 sources in the corpus run validated against this schema
(`docs/knowledge/evidence/m2-002-corpus-report.json`). The first run caught 40
lesson-note records whose `source_uri` contained unencoded spaces from the file
names. The schema rejected them as intended, and the corpus tool now
percent-encodes.

## 9. How each component uses the contract

| Component | Uses |
|---|---|
| Ingestion (M2-002) | Builds and validates the record; stores it with the version; refuses a record that fails validation (visible, retryable with a metadata patch). |
| Chunking (M2-003) | Copies the flat record onto every chunk; adds the chunk `Locator` and chunk-level curriculum position. |
| Retrieval | Filters on `approval_status`, effective window, `access_classification` and `scope` (`access.retrieval_filter`); weights on `authority_tier`. |
| Provenance | Renders citations from `title` and `Locator` (`p. 10 [printed VIII], Module 12.1: Protein Sequencing`). |
| Evaluation | Groups results by `source_type`, `subject`, grade and tier. |

## 10. Open items for governance

1. **Approver of the baseline set.** The 72 baseline records carry
   `approved_by: content-team (NEXA-M0-004 baseline set; owner confirmation pending)`.
   The content owner should confirm or replace this before production.
2. **Lesson notes classification.** The `sNN_cmNNNN` DOCX set is classified as
   EduNeX LMS content (tier 4). If NDoE publishes them as official lesson notes,
   reclassify to `lesson_notes` (tier 3).
3. **Teacher guides default to `teacher_only`.** v0 served teacher guides to
   students. Confirm this tightening.
4. **Permitted external domains.** Confirm the allowlist in 5.1.
5. **School claim.** School and class scoped retrieval needs a `school_id`
   claim from identity (NEXA-M1-005 SSO seam).
6. **Source inconsistencies found** (for the content team; ingestion keeps
   both values and flags them): Physics Term 3 syllabus Module 12.1 lists
   outcomes coded `12.2.3x`, and 9 chunks in 4 documents carry `code_mismatch`;
   the `s03` lesson note is filed under module 12.3 although it is Genetics
   (12.2); one document spells `Biotehcnology`; `s01` spells `Sciencetific`.
