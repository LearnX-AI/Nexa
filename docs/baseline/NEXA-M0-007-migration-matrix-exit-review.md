# NEXA-M0-007: v0 to v1 Migration Matrix and M0 Exit Review

Status: Draft for governance decision
Milestone: M0 (formal exit record)
Owners: Technical Lead Sehan Yasaru Rathnasooriya; Security review contributor
Manuja Peiris; Program approval Chandana Silva

## 1. Purpose

Consolidate the verified findings from NEXA-M0-001 through NEXA-M0-006 into one
current-state record, confirm the v0 to v1 migration decisions, review the ADR
set, and recommend whether NEXA is ready to proceed into M1. Final approval is a
governance decision and is not inferred from task completion.

## 2. Inputs and their state

| Input | Covers | Location | State |
|---|---|---|---|
| NEXA-M0-001 | v0 technology stack | prior M0 record | Referenced |
| NEXA-M0-002 | verified as-built architecture | docs/baseline/NEXA-M0-002 | Complete |
| NEXA-M0-003 | repo, codebase and dependency baseline | docs/baseline/NEXA-M0-003 | Complete |
| NEXA-M0-004 | knowledge, RAG and data inventory | docs/baseline/NEXA-M0-004 | Complete |
| NEXA-M0-005 | security, secrets and access baseline | prior M0 remediation | Referenced, one new finding |
| NEXA-M0-006 | evaluation, QA and safety baseline | docs/baseline/NEXA-M0-006 | Complete |

M0-001 and M0-005 were carried out earlier in the program (the secrets and CORS
hardening on dev-Tharushi, commit 07d5258). They are referenced here. One new
security finding (section 6) was raised by M0-004 and must be reflected back
into M0-005.

## 3. Migration matrix

Format per component: current v0 state, evidence, verification status, v1
decision, required action or ADR. Evidence points at main.py unless noted.

| Component | Current v0 state | Evidence | Verify | v1 decision | Action / ADR |
|---|---|---|---|---|---|
| FastAPI core service | One monolith, app at 4169, ~20 routes | main.py:4169 | VERIFIED | REPLACE | Split per M1-001; ADR-0001, ADR-0003 |
| Chat orchestration | /chat loop | main.py:5016 | VERIFIED | REPLACE | Agent Runtime; ADR-0003 |
| Intent classification and routing | In-code classify_intent | main.py:856, 921 | VERIFIED | IMPROVE | Agent selection in runtime; Safety routing later |
| Local LLM and runtime | ChatOllama llama3.1:8b-instruct-q5_K_M | main.py:1471, 3960 | VERIFIED | IMPROVE | Model Gateway ollama backend; ADR-0001 |
| General-knowledge path | OpenAI gpt-4o temp 0.3 | main.py:180 | VERIFIED | IMPROVE | Model Gateway openai backend; ADR-0001 |
| Curriculum RAG | Chroma curriculum_db cosine | main.py:3952 | VERIFIED | IMPROVE | Knowledge Service; ADR-0004 |
| Vector database | Chroma persisted store | main.py:3954 | VERIFIED | REPLACE | pgvector; ADR-0002 |
| LMS and protected ingestion | /ingest-material | main.py:4705 | VERIFIED | IMPROVE | Knowledge Service ingestion; ADR-0004 |
| Document extraction and OCR | PyPDFLoader, python-docx; OCR experiments only | main.py:53; Prep_files | PARTIALLY VERIFIED | IMPROVE | Confirm OCR before relying; ADR-0004 |
| Source authority and ranking | doctype priority, syllabus first, distance re-rank | main.py:1395, 1906 | VERIFIED | IMPROVE | Knowledge Service; ADR-0004 |
| Provenance and citations | chunk metadata surfaced | main.py:1998 | VERIFIED | IMPROVE | Knowledge Service; ADR-0004 |
| Image generation | Remote Qwen on Ilaibu DGX 100.92.95.83:9000 | main.py:1493, 4108 | VERIFIED | IMPROVE | Tool Gateway; ADR-0006; confirm worker code (qwain.py) |
| Mathematics engine | SymPy exact compute | main.py:2337 onward | VERIFIED | IMPROVE | Tool Gateway maths tool |
| Student and teacher roles | Inferred from email domain | main.py:3632, 3642 | VERIFIED | IMPROVE | Identity roles enforced in runtime; ADR-0005 |
| Identity, auth, RBAC | None. Email and name trusted from the request | main.py:4625, 5020 | VERIFIED | REPLACE | Identity service, signed tokens, can(); ADR-0005 |
| Session and conversation memory | In-process dicts plus MySQL history | main.py:5026; schema | VERIFIED | IMPROVE | Memory Service; ADR-0002 |
| Share links | nexa_shared_chats | main.py:3306 | VERIFIED | KEEP | Memory Service |
| Logging and audit | print plus chat log rows, no structure, no trace | main.py | VERIFIED | IMPROVE | Observability library and sink |
| School and core sync | Owned by EduNeX sync tooling | out of service | VERIFIED | KEEP | No NEXA change |
| Databases and schemas | MySQL nexa_ai | nexa_mysql_setup.sql | VERIFIED | REPLACE | PostgreSQL; ADR-0002 |
| Public exposure | uvicorn 0.0.0.0:8000 behind Cloudflare tunnel; CORS EduNeX | main.py:4173 | VERIFIED | IMPROVE | API Gateway behind tunnel; ARCH-001 |
| Secrets and credentials | Env from root-owned files, DB password from os.environ | main.py:1495 | VERIFIED | REPLACE | Keep pattern, extend to all services; see M0-005 |
| QA, evaluation and safety | None formal; star ratings only | M0-006 | VERIFIED | NEW IN v1 | Safety and Evaluation service |
| Web retrieval and language | duckduckgo, wikipedia, Tok Pisin references | requirements.txt; main.py | PARTIALLY VERIFIED | IMPROVE | Tool Gateway behind policy |
| Edge and offline | None | n/a | NOT IMPLEMENTED | NEW IN v1 | NEXA Mini; ADR-0007 |
| Model lifecycle and version governance | None | n/a | NOT IMPLEMENTED | NEW IN v1 | Model Gateway registry and governance |
| Prompt and policy governance | Prompts inline in main.py | main.py | VERIFIED | IMPROVE | Runtime and Safety |
| Observability and evaluation services | None | n/a | NOT IMPLEMENTED | NEW IN v1 | Observability and Safety |
| Tool and Connector Gateway and MCP | None | n/a | NOT IMPLEMENTED | NEW IN v1 | Tool Gateway; ADR-0006 |

## 4. v1 build progress at exit (context)

M1 foundation work has proceeded in parallel on nexa-v1-core and is recorded
here for context, not as an M0 deliverable: Model Gateway (8100), Identity and
RBAC (8101), Agent Runtime (8102) and API Gateway (8099) are implemented, and an
Observability library is specified. This does not substitute for the governance
exit decision in section 8.

## 5. Open issues and risks

- Architecture: the live v0 service runs from a machine working copy under
  /home/admin/Nexa, not a checkout of this repository. Owner: Technical Lead.
- Security: see section 6.
- Data and privacy: no retention policy for learner data; generated media stored
  as base64 and blobs inside the chat log row. Owner: Security and Platform.
- Models: two disagreeing dependency manifests and an unpinned main manifest;
  the image stack (torch) is listed on the main service though it runs remotely.
  Owner: Technical Lead.
- RAG and knowledge: OCR and scheduled re-index not confirmed in the live path.
  Owner: AI engineering.
- Infrastructure: image worker code (qwain.py) not confirmed to match what runs
  on the Ilaibu DGX. Owner: Platform.
- Integration: Tool and MCP direction not yet designed in detail (ADR-0006
  Proposed). Owner: Platform.
- QA and evaluation: no formal QA or safety benchmarks exist (M0-006). Owner: AI
  engineering with security review.
- Offline and edge: not started. Owner: to assign.
- Documentation: no project licence; committed backup copies at the repo root.
  Owner: Technical Lead.

## 6. Security carry-over to M0-005

- Addressed earlier: database password moved to os.environ with no default,
  secrets in root-owned EnvironmentFiles, CORS locked to EduNeX, database
  password rotated (dev-Tharushi 07d5258).
- New, not yet addressed: user_memory.json stores real user email addresses and
  their questions in plaintext and is committed to the repository
  (NEXA-M0-004 section 10). This is an unresolved personal-data exposure,
  possibly including minors, and it is the one urgent item that is identified
  but not yet contained. Required action: remove from the working tree and git
  history, stop writing per-user facts to a flat file, and move the capability
  into the Memory Service with access control and retention.

## 7. ADR review

Evidence is sufficient to accept the five foundation ADRs and to keep ADR-0006
Proposed. An ADR is not marked Accepted merely to close M0.

| ADR | Decision support from evidence | Status |
|---|---|---|
| ADR-0001 Model Gateway | v0 constructs model clients inline with no single entry; a gateway is clearly needed and is built | Accepted |
| ADR-0002 Primary data and vector platform | v0 uses Chroma and MySQL; consolidation on PostgreSQL and pgvector is justified | Accepted |
| ADR-0003 Agent Runtime | v0 orchestration is a monolith loop; a runtime boundary is needed and is built | Accepted |
| ADR-0004 Knowledge and RAG | v0 RAG works but is coupled; a governed Knowledge Service is justified | Accepted |
| ADR-0005 Identity and RBAC | v0 has no authentication and trusts a client-supplied email; this is the strongest driver in the baseline | Accepted |
| ADR-0006 Tool Integration and private MCP | Not yet designed in detail; image and maths and web are candidate tools but the MCP surface is unspecified | Proposed, keep open |

## 8. M0 exit criteria check

| Criterion | Met |
|---|---|
| Technology stack verified | Yes (M0-001, M0-002) |
| As-built v0 architecture documented | Yes (M0-002) |
| Active repositories, services, dependencies known | Yes (M0-003) |
| Data, RAG, knowledge stores and ingestion inventoried | Yes (M0-004) |
| Critical security and secrets issues identified, urgent exposures addressed or contained | Partly. Secrets and CORS addressed; user_memory.json exposure identified but not yet contained |
| QA, evaluation and safety baseline documented | Yes (M0-006) |
| v0 to v1 migration matrix complete | Yes (section 3) |
| Documentation discrepancies corrected or recorded | Yes (M0-003, this doc) |
| Required ADR decisions for M1 resolved | Yes for ADR-0001 to 0005; ADR-0006 Proposed and not required for current M1 foundation |
| No critical unknown prevents safe M1 foundation | Yes, with the one privacy exposure as a condition |

## 9. Exit decision

Recommendation: M0 CONDITIONAL. Proceed to M1 foundation work, conditional on
clearing one blocker before M0 is formally signed off:

- Contain the user_memory.json personal-data exposure: remove it from the
  working tree and git history, stop flat-file per-user fact capture, and move
  the capability into the Memory Service with access control and retention.

All other exit criteria are met. The ADR set required for the current M1
foundation (ADR-0001 to 0005) is accepted; ADR-0006 remains Proposed and is not
a blocker for the foundation services. Final approval to proceed is a governance
decision by the program approver (Chandana Silva) and must not be inferred from
the completion of these M0 tasks.

## 10. Core principle

Preserve what works, modernize what limits scale, security or maintainability.
