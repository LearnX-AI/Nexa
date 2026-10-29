# NEXA-M0-004: Knowledge, RAG and Data Inventory

Status: Verified against code and data assets on branch nexa-v1-core
Milestone: M0
Evidence base: main.py, Prep_files/, nexa_mysql_setup.sql, repo root document
sets, user_memory.json

## 1. Scope

Baseline every current knowledge and data asset: curriculum source sets, the RAG
pipeline, the vector store, LMS ingested materials, source priority rules, the
re-index process, the chat and log databases, generated image blobs, user and
session data, backup locations and retention. Data is separated by class in
section 8, with sensitivity, owner, retention, backup and a v1 recommendation.

## 2. Curriculum source document sets (implemented)

- At the repository root: 29 PDF files and 41 .docx files. The PDFs are the PNG
  STEM senior curriculum (Biology, Chemistry, Physics syllabus, teacher guides
  and student guides for Grade 11 and Grade 12). The .docx files are Grade 12
  lesson notes across Genetics, Biotechnology, Bioenergetics, Battery
  Technology, Health and Medical, Neuroscience, Robotics, Population Ecology and
  Evolution.
- These are the base knowledge set embedded into the vector store at first
  build.

## 3. Ingestion and parsing pipeline

- The working pipeline scripts live in Prep_files/: chunking.py, ingestion.py,
  bulk_pipeline.py, pdf_report.py, pipeline_metadata.py, retrieval_filters.py,
  text_processing.py, ai_processing.py, plus OCR experiments (ocr_test.py,
  ocr_test_ai.py, test_easyocr.py) and retrieval_test.py.
- In the live service, PDF text is loaded with PyPDFLoader (main.py:53) and
  .docx with python-docx. Uploaded documents and read-url content follow the
  same path (`/upload` main.py:4179, `/read-url` main.py:4623).
- OCR exists as experiments in Prep_files/ (easyocr based), not confirmed wired
  into the live main.py path. Mark OCR PARTIALLY VERIFIED.

## 4. Chunking, embedding and vector store

- Chunking: RecursiveCharacterTextSplitter, chunk_size 1500, chunk_overlap 300
  (main.py:3992 and 4739). The same settings are used for base documents and for
  runtime ingested text.
- Embedding model: nomic-embed-text served by Ollama, wrapped as
  PrefixedOllamaEmbeddings to add task prefixes (main.py:1472, 3933, 3944).
- Vector store: Chroma, collection curriculum_db, cosine space
  (`hnsw:space cosine`), persisted to disk at ~/Nexa/chroma_store (CHROMA_DIR,
  main.py:1890, 3954). The persisted store holds both the base PDFs and every
  LMS document ingested since, so a restart retains all of them
  (main.py:3946 to 3958).

## 5. LMS and protected content ingestion

- `/ingest-material` (main.py:4705) ingests LMS and protected content into the
  same Chroma collection during runtime. Documents added by the ingest endpoint
  persist across restarts (main.py:1884, 3947).

## 6. Source authority, ranking and provenance

- Each chunk carries metadata: subject, grade, term, doctype and a numeric
  priority (main.py:168, 1402, 1412, 1427).
- Document type priority: the syllabus outranks the guides during retrieval,
  with the syllabus given priority 0 as the outcome authority (main.py:1395 and
  the DOCTYPE_PRIORITY map).
- Retrieval merges candidates then re-ranks the merged pool by vector distance
  so the best chunks lead, whether they came from base PDFs or LMS ingestion
  (main.py:1906). There is no separate learned reranker model.
- Provenance: source tags and subject or grade summaries are tracked for the
  answer (main.py:1998). Citations are derived from chunk metadata.

## 7. Re-index process

- There is no scheduled re-index. The store is built once, then grows through
  the ingest endpoint at runtime. The persisted directory is the durable index.
  A full rebuild would mean clearing chroma_store and re-ingesting, which is a
  manual operation today. Mark re-index PARTIALLY VERIFIED (runtime add verified,
  scheduled rebuild not implemented).

## 8. Data inventory by class

### Curriculum knowledge
- Assets: root PDFs and .docx, the Chroma curriculum_db store.
- Source: PNG curriculum and EduNeX content team.
- Sensitivity: low (official curriculum material).
- Owner: content team.
- Retention: indefinite, versioned with the curriculum.
- Backup: chroma_store on the DGX; source documents in the repository.
- v1 recommendation: move to the Knowledge Service on pgvector (ADR-0002 and
  ADR-0004), keep the base document set under version control, add a defined
  re-index job.

### Learner data (high sensitivity)
- Assets:
  - MySQL nexa_chat_logs: user_email, user_name, user_prompt, nexa_response,
    session_id, timestamps and ratings (nexa_mysql_setup.sql).
  - user_memory.json: a plaintext JSON file, committed in the repository, that
    stores per user captured facts keyed by real email address. It contains
    actual user identities and their questions.
- Source: end users (students and staff) through the chat.
- Sensitivity: high. This is personal data and may include minors.
- Owner: platform, under the data protection policy.
- Retention: none defined today. Finding.
- Backup: MySQL backup regime (ops owned); user_memory.json has no managed
  backup and should not exist in the repository at all.
- v1 recommendation: the Memory Service owns learner data on PostgreSQL
  (ADR-0002) with a defined retention policy and access control. See the data
  privacy finding in section 10.

### Operational logs
- Assets: stdout print logging; ratings (stars) on chat logs.
- Sensitivity: medium (log lines can echo prompts).
- v1 recommendation: structured JSON logs via the Observability library with a
  trace id, shipped to one sink, with prompt content excluded from logs.

### Generated content
- Assets: generated images stored inside nexa_chat_logs as image_base64
  (LONGTEXT) and image_blob (LONGBLOB), with mime type and filename columns
  (nexa_mysql_setup.sql, main.py:2277, 3282).
- Sensitivity: low to medium.
- v1 recommendation: store generated media in object storage with a reference in
  the database, not as base64 and blobs inside the chat log row.

### Analytics data
- Assets: star ratings on nexa_chat_logs, nexa_faq_answer map.
- Sensitivity: low.
- v1 recommendation: feed into the Safety and Evaluation subsystem as quality
  signal.

## 9. Databases and schemas

- MySQL database nexa_ai (nexa_mysql_setup.sql).
- nexa_chat_logs: the primary log table, keyed by log_id, indexed on
  session_id, user_email and timestamp, with the image columns noted above.
- nexa_shared_chats: share link records (main.py:3306).
- nexa_faq_answer: a small fixed answer map (main.py:3215).
- v1 recommendation: migrate to PostgreSQL per ADR-0002, split learner data and
  operational data, and add retention.

## 10. Open items and findings

- DATA PRIVACY, high: user_memory.json stores real user email addresses and
  their captured questions in plaintext and is committed to the repository. This
  exposes personal data, possibly including minors, in version control. Action:
  remove it from the working tree and from git history, stop writing per user
  facts to a flat file, and move this capability into the Memory Service with
  access control and retention. Owner: security and platform. Carry to
  NEXA-M0-007 and cross reference NEXA-M0-005.
- Retention: no retention policy exists for learner data or logs. Define one in
  v1.
- Generated media stored as base64 and blobs inside the log row bloats the
  table. Move to object storage in v1.
- OCR and scheduled re-index are not confirmed in the live path. Resolve before
  relying on them in v1.

## 11. Verification status

| Item | Status |
|---|---|
| Base curriculum document sets | VERIFIED (repo root) |
| Chunking 1500/300 | VERIFIED (main.py:3992, 4739) |
| nomic-embed-text embeddings with prefixes | VERIFIED (main.py:3933, 3944) |
| Chroma curriculum_db cosine, persisted | VERIFIED (main.py:3954, CHROMA_DIR) |
| Runtime LMS ingestion persists | VERIFIED (main.py:3946 to 3958) |
| Syllabus outranks guides, distance re-rank | VERIFIED (main.py:1395, 1906) |
| MySQL schema and image columns | VERIFIED (nexa_mysql_setup.sql) |
| user_memory.json plaintext learner data in repo | VERIFIED |
| OCR in live path, scheduled re-index | PARTIALLY VERIFIED |
