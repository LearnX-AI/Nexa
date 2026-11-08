# NEXA-M2-002: Document Ingestion, Parsing and OCR Pipeline

Status: Draft for governance acceptance
Milestone: M2
Pipeline version: 2.0.0
Aligns with: NEXA-ADR-0002, NEXA-ADR-0004, NEXA-GOV-001, NEXA-MIG-001
Implementation: `v1/knowledge/ingestion/`, endpoints in `v1/knowledge/main.py`
Evidence: `docs/knowledge/evidence/m2-002-corpus-report.json`

## 1. Stages

```
upload / LMS push
   │
validate ──► quarantined (hostile)  /  failed (unsupported, empty, too large)
   │  original stored by checksum (content-addressed, write-once)
extract ──► failed, retryable (parser error, no usable text)
   │  per page: native text, or OCR when the text layer is missing or garbled
scan ─────► quarantined (credentials, key material, learner records)
   │
metadata ─► failed, retryable with a metadata patch (M2-001 validation)
   │  duplicate (same bytes already registered under another source_id)
chunk ────► failed, retryable
   │  run + chunk set saved in one transaction; older versions superseded
index ────► failed, retryable (embedding through the Model Gateway)
   │  only approved sources are indexed
done
```

Every job records its `state`, the `stage` it reached, `attempts`, `retryable`,
`error_code`, `error_detail` and `warnings`. Jobs are listed, inspected and
retried through the API (section 7).

## 2. Formats

| Format | Extractor | Structure kept | Page provenance |
|---|---|---|---|
| PDF (text layer) | pypdf | Lines, rebuilt into headings, paragraphs, lists, exercises, equations (M2-003) | Physical page, plus the printed page label read from the page (`VIII \| Page`, `Page 12`) |
| PDF (scanned) | pdfium render at 300 dpi + tesseract | As above, from OCR lines | Physical page; per-page OCR confidence |
| DOCX | python-docx, walking the XML in reading order | Heading styles and levels, list paragraphs, tables with rows, OMML equations, image alt text, text boxes (read once) | Word's last rendered page breaks, marked `rendered_estimate` |
| PPTX | python-pptx | Slide titles, bullets with levels, tables, grouped shapes, speaker notes | Slide number |
| LMS / EduNeX | HTML parser | h1 to h6, paragraphs, list items, tables; script and style dropped | None (`page_basis: none`); `source_uri` carries course, item and revision |
| PNG, JPEG, TIFF | tesseract | OCR lines | Frame number. Disabled on Pillow below 13 (section 8). |

Every run records the extractor and library versions (`pypdf 6.21.4`,
`python-docx 1.2.0`, `python-pptx 1.0.2`) and, when OCR ran, the engine block:
`tesseract 5.3.4`, language `eng`, 300 dpi, `--oem 1 --psm 3`, low-confidence
threshold 70. The same bytes with the same versions always give the same text.

Legacy binary `.doc` and `.ppt` are refused with a request to convert.

## 3. Validation and content safety

| Control | Behaviour |
|---|---|
| Size | Over `NEXA_KNOWLEDGE_MAX_FILE_MB` (60) is refused (HTTP 413 at upload). Page limit 1,500. |
| Type | Magic bytes and container contents must match an allowed type and the extension. A PDF named `.docx` is refused. |
| PDF active content | `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile`, `/RichMedia`, `/XFA`, `/SubmitForm`, `/ImportData` are refused, including names hidden with `#xx` escapes (`/J#61vaScript`). Encrypted PDFs are refused. |
| Office active content | Macros (`vbaProject.bin`), ActiveX, externally loaded templates or objects are refused. |
| ZIP containers | Entry count, total uncompressed size and per-member ratio limits (zip bombs); path traversal names refused. |
| Malware | ClamAV through `clamdscan` when `NEXA_KNOWLEDGE_AV=clamav`. With `NEXA_KNOWLEDGE_REQUIRE_AV=true` (set in QA, UAT, PROD) a missing or failing scanner refuses the upload. DEV records `skipped:not_configured` as a job warning. |
| Credentials | Extracted text containing private keys, AWS keys, GitHub, OpenAI or Slack tokens, JWTs or `password=` style assignments is quarantined and never chunked. |
| Learner records | 10 or more distinct email addresses in one document is quarantined as likely learner data, which belongs in the Memory Service. Fewer give a warning. |
| Rejected bytes | Not stored. Quarantined jobs are not retryable. |

## 4. OCR

- A PDF page is OCRed when its text layer has fewer than 60 characters or is
  garbled (replacement characters, `(cid:N)` runs, private-use glyphs).
- OCR text replaces the native text only when it is longer, and not when it is
  low-confidence noise over a page that already had some text (diagram pages).
- Page status: `ok`, `ocr`, `low_confidence` (below 70), `empty` (blank or
  diagram-only), `failed`. A document with any low-confidence or failed page is
  `partial`, and the pages are listed in the job warnings. A document with no
  usable page fails as `no_usable_text`.
- Chunks carry `min_ocr_confidence` and a `low_confidence` flag, so retrieval
  and review can treat them differently.

## 5. Idempotency and versioning

| Situation | Result |
|---|---|
| Same bytes, same manifest, same pipeline versions | The existing job is returned (`created: false`). Nothing is written. |
| Same bytes under another file name | Same idempotency key, same job. |
| Same bytes under another `source_id` | Job ends `duplicate`, pointing at the existing version. |
| Same bytes, new chunking config or new pipeline, normalizer, chunker or extractor version | New run; the new chunk set becomes active and the old one is deactivated, never mixed. Still one source version. |
| New bytes for an existing `source_id`, approved | New version. Older versions become `superseded`, their chunks are deactivated and their vectors removed from the store. |
| Approval or metadata fix | `POST /jobs/{id}/retry` with `metadata_patch` re-processes from the stored original. |

Chunk ids are a hash of version, pipeline, normalizer and extractor version,
chunker version, config hash and ordinal, so they are stable for the same
inputs and different for anything else.

## 6. Storage

- Registry: sources, runs, jobs, chunks. SQLite in DEV and single-host QA
  (`NEXA_KNOWLEDGE_REGISTRY=sqlite:///...`), in-memory for tests. The
  PostgreSQL schema for ADR-0002 is `v1/knowledge/migrations/0001_knowledge_registry.sql`;
  its adapter lands with the pgvector retrieval step.
- Originals: content-addressed, write-once, mode 0440 under
  `NEXA_KNOWLEDGE_BLOB_DIR`, referenced as `blob://sha256/<hex>`. Retries and
  reprocessing never need the upload again. Production points the same
  interface at object storage.
- No credentials, tokens or sensitive files are written into jobs, records or
  the repository. `source_uri` values with credentials or signed parameters are
  rejected by the schema.

## 7. API (Knowledge Service, port 8103)

| Endpoint | Permission | Purpose |
|---|---|---|
| `POST /v1/knowledge/sources` | `knowledge.ingest` | Multipart upload, `manifest` JSON (`metadata`, `chunking`). 202 with the job. |
| `POST /v1/knowledge/lms-items` | `knowledge.ingest` | EduNeX or LMS push: `lms` reference, `title`, `body` (HTML or text), `metadata`. |
| `GET /v1/knowledge/jobs?state=` | `knowledge.ingest` | List jobs, e.g. all `failed` or `quarantined`. |
| `GET /v1/knowledge/jobs/{id}` | `knowledge.ingest` | Job state, stage, errors, warnings. |
| `POST /v1/knowledge/jobs/{id}/retry` | `knowledge.ingest` | Retry; optional `metadata_patch` (approval, field fixes). |
| `GET /v1/knowledge/sources?status=` | `knowledge.query` | Registered versions. |
| `GET /v1/knowledge/sources/{version_id}` | `knowledge.query` | Record plus every run with stats and quality. |
| `GET /v1/knowledge/sources/{version_id}/chunks?active=` | `knowledge.ingest` | Chunk review. |
| `POST /v1/knowledge/sources/{version_id}/reindex` | `knowledge.ingest` | Re-embed one approved, active version, or `all`. |
| `GET /v1/knowledge/schema/source-metadata` | authenticated | The M2-001 JSON Schema. |
| `POST /v1/knowledge/query` | `knowledge.query` | Retrieval, now filtered by approval, effective dates, access class and scope, with a `citation` on each result. |

Every decision emits an audit event with the trace id.

## 8. Security exception: Pillow on Python 3.10

Pillow 12.3.0 is the last release supporting Python 3.10, the v1 runtime.
pip-audit reports 39 advisories against it, all fixed in Pillow 13.0.0, which
requires Python 3.11+. Python 3.10 reached end of life in October 2026.

Decision (2026-11-02): stay on Python 3.10 with a time-boxed exception.
- `v1/security/audit-exceptions.json` lists the 39 advisory ids for
  `pillow 12.3.0` only, expiring 2027-01-31.
- `v1/tools/audit_gate.py` replaces the plain pip-audit step in CI. It fails
  on any advisory not listed for that exact version, on an expired exception,
  and on a stale one (version no longer pinned).
- Mitigation: untrusted images never reach Pillow's decoders. Direct
  PNG/JPEG/TIFF uploads are refused on Pillow below 13
  (`NEXA_KNOWLEDGE_IMAGE_UPLOADS=auto`). PDF pages are rendered by pdfium into
  raw bitmaps. DOCX and PPTX images are not decoded.
- Exit: move the v1 runtime to Python 3.12, pin Pillow 13+, delete the
  exception. Image uploads then turn on automatically.

## 9. Test corpus results

Run with `python v1/tools/knowledge_corpus.py --out <dir>`.

| Set | Files | Result |
|---|---|---|
| Native PDFs (syllabi, teacher guides, student guides; Biology, Chemistry, Physics; Grades 11 and 12) | 29 | 29 succeeded |
| Scanned PDF (3 real syllabus pages rasterised, last page degraded to 60 dpi, skewed and blurred) | 1 | partial: pages 1 and 2 OCR at 94.7 confidence, page 3 flagged `low_confidence` at 51.7 |
| DOCX lesson notes | 41 | 41 succeeded, tables and text boxes included |
| PPTX teacher deck (built from a real lesson note) | 1 | succeeded: slides, bullets, table, speaker notes |
| **Total** | **72** | **71 succeeded, 1 partial (as designed), 0 failed**; 1,811 chunks in 61 s on one CPU |

The repository has no real scanned PDFs or decks, so those two files are
generated from real curriculum material by the corpus tool. Nothing binary is
committed for them.

Idempotency, measured on the same run:

| Check | Result |
|---|---|
| Re-submit all 72 files | 0 new jobs, sources, runs or chunks |
| Reprocess one file with a new chunking config | 1 source version, 2 runs, the new chunk set active, the old deactivated |
| Failures visible and retryable | Proven by tests: metadata failure fixed by patch, corrupt file retried until `max_attempts`, index failure retried, quarantine not retryable |

Content coverage per run (share of extracted content words present in the
chunk set): mean 99.7%, minimum 98.3% (the degraded scan page).

## 10. Operations

DEV host prerequisites:

```
sudo apt-get install -y tesseract-ocr tesseract-ocr-eng clamav-daemon
sudo install -d -o admin -m 0750 /var/lib/nexa/knowledge /var/lib/nexa/knowledge/originals
pip install -r v1/knowledge/requirements.txt
```

New settings are listed in `v1/knowledge/.env.example`. Set
`NEXA_KNOWLEDGE_REGISTRY` and `NEXA_KNOWLEDGE_BLOB_DIR` to disk paths so
registry and originals survive restarts. Since M2-004 the vector store is
pgvector and persists; see NEXA-M2-004 for index versions and model changes.
With the in-memory store (tests only), set `NEXA_KNOWLEDGE_REINDEX_ON_START=true`
or call `POST /v1/knowledge/sources/all/reindex` after a restart.

## 11. Known limitations

- PDF tables are flattened to text by pypdf; their words are retained but the
  row structure is not. DOCX and PPTX tables are fully structured.
- DOCX page numbers come from Word's last rendered layout and are marked
  approximate.
- OCR language is English. Tesseract has no Tok Pisin or Hiri Motu model; text
  in those languages OCRs with the Latin English model and lower confidence.
- LMS synchronisation is push only (`/lms-items`). A scheduled pull from the
  EduNeX API is part of the M2 synchronisation work, not this task.
- Some source PDFs have broken spacing in their own text layer (`disea se`,
  `l ower`). It is preserved rather than guessed at.

## 12. Acceptance criteria

| Criterion | Evidence |
|---|---|
| Test corpus covering native PDFs, scanned PDFs, DOCX and PPTX processed | Section 9; corpus report |
| Failures are visible and retryable | Sections 1 and 7; `test_metadata_failure_is_visible_and_retryable_with_patch`, `test_corrupt_file_fails_at_extract_and_bounded_retries`, `test_index_failure_is_visible_and_retryable` |
| Source-page provenance survives ingestion | Locator on every chunk; 100% of chunks carry a page, 68.6% a printed page label (M2-003 benchmark) |
| Pipeline is idempotent and versioned | Section 5; idempotency results in section 9; `test_ingest_is_idempotent_and_records_provenance`, `test_reprocess_with_new_config_creates_run_not_duplicates` |
| No credentials or sensitive files embedded | Section 3; `test_hostile_and_sensitive_files_are_quarantined`; schema rejects credentialed URIs |
