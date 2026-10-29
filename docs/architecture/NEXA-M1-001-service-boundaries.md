# NEXA-M1-001: Core Service Boundaries and Repository Structure

Status: Draft for review
Milestone: M1
Derived from: NEXA-ARCH-001 and accepted ADR-0001 through ADR-0005 (ADR-0006 Proposed)
Branch: nexa-v1-core

## 1. Purpose

Define the v1 repository and service structure, the ownership of each service,
the rules that keep services decoupled, the shared library policy, the
environment configuration pattern, and the mapping from the v0 monolith to the
v1 services. This is the structural contract that all later M1 and M2 work lands
inside.

## 2. Principle

Preserve what works, modernize what limits scale, security or maintainability.
Every capability in v0 maps to exactly one v1 owner. No service reaches into
another service's internals. All model traffic goes through the Model Gateway,
all external entry goes through the API Gateway, and all identity decisions go
through the Identity and policy layer.

## 3. Service map

Nine logical units. Four are implemented as running services on nexa-v1-core
today; the rest are defined here as boundaries and are built in later M1 and M2
steps.

| Service | Role | State | Port |
|---|---|---|---|
| API Gateway | Single external entry. Edge token check, CORS, routing to the runtime. | Implemented | 8099 |
| Model Gateway | One OpenAI compatible entry to every approved model, logical model registry, trace stamping. | Implemented | 8100 |
| Identity and Policy | Signed tokens, roles to permissions, `can()` checks, EduNeX SSO seam. | Implemented | 8101 |
| Agent Runtime | Hosts named agents, enforces the agent permission, routes model calls through the gateway. | Implemented | 8102 |
| Knowledge Service | Governed RAG: ingestion, chunking, embedding, retrieval, source authority, citations. | Boundary defined | 8103 (reserved) |
| Memory Service | Session and conversation memory, chat history, share links. | Boundary defined | 8104 (reserved) |
| Tool and Integration Gateway | Governed tool calling over a private MCP surface (ADR-0006). | Boundary defined | 8105 (reserved) |
| Safety and Evaluation | Guardrail checks, evaluation harness, quality and regression gates. | Boundary defined | 8106 (reserved) |
| Observability (shared) | One JSON log format and one trace id scheme across every service. | Shared library | n/a |

Reserved ports are recorded here so later services do not collide with the four
that are live. They are not open until the service exists.

## 4. Repository tree

Single repository, one top level package per service under `v1/`, shared code
under `v1/shared/` and `v1/observability/`, deployment manifests under
`v1/deploy/`, and documentation under `docs/`.

```
nexa/                         repo root (v0 monolith still lives here until retired)
  main.py                     v0 FastAPI monolith (frozen, see migration map)
  index.html                  v0 single page frontend (frozen)
  requirements.txt            v0 dependencies (frozen)
  docs/
    architecture/             ARCH, ADR and M1 structural docs (this file)
    baseline/                 M0 as-built, inventory and exit records
  v1/
    api_gateway/              edge entry (implemented)
    model_gateway/            model access (implemented)
      backends/               ollama, openai backends behind one interface
    identity/                 tokens, rbac, sso seam (implemented)
    agent_runtime/            agent hosting (implemented)
    knowledge/                RAG service (to build)
    memory/                   session and history service (to build)
    tool_gateway/             private MCP tool surface (to build, ADR-0006)
    safety_eval/              guardrails and evaluation (to build)
    observability/            shared logging and tracing (shared library)
    shared/                   shared contracts and small helpers (shared library)
    deploy/                   systemd units and ops helpers
```

The v0 files stay in place and are frozen. They are removed only when
NEXA-MIG-001 confirms the v1 service that replaces each one is in production.

## 5. Service ownership map

Each service has one technical owner accountable for its contract, its tests and
its deployment. Ownership is recorded here and kept in sync with the program
roster.

| Service | Owner role | Accountable for |
|---|---|---|
| API Gateway | Platform engineering | Edge auth, CORS policy, routing, rate limits (later) |
| Model Gateway | Platform engineering | Model registry, backend adapters, trace block |
| Identity and Policy | Security engineering | Token issue and verify, RBAC map, SSO seam |
| Agent Runtime | AI engineering | Agent registry, prompts, permission enforcement |
| Knowledge Service | AI engineering | Ingestion, embeddings, retrieval, citations |
| Memory Service | Platform engineering | History schema, retention, share links |
| Tool and Integration Gateway | Platform engineering | Tool registry, MCP surface, tool authz |
| Safety and Evaluation | AI engineering with security review | Guardrails, eval datasets, regression gates |
| Observability (shared) | Platform engineering | Log schema, trace id scheme |

Named owners are maintained in the program tracker, not in source, so this file
does not go stale on a personnel change.

## 6. Import and dependency rules

The rules that keep services decoupled and testable.

1. No service imports another service's internal modules. A service calls
   another only over its HTTP contract.
2. The only shared imports allowed are `v1.shared` (contracts and small pure
   helpers) and `v1.observability` (logging and tracing). These two carry no
   business logic and no service state.
3. `v1.identity` is a special shared case: other services may import its pure
   token and RBAC helpers (`verify_token`, `can`, `Principal`) because identity
   is a cross cutting policy concern, but they must not import its service app
   or its issuance path. Only the Identity service issues tokens.
4. Direction of calls is fixed: API Gateway to Agent Runtime to Model Gateway to
   a backend. The Agent Runtime and later the Knowledge and Tool services call
   the Model Gateway. Nothing calls upstream against this direction.
5. No model SDK (ollama, openai) is imported anywhere except inside
   `v1/model_gateway/backends/`. Every other service uses the gateway's HTTP
   contract.
6. No service imports the v0 `main.py` or any v0 module. v0 and v1 share nothing
   in process.

A simple CI check greps for forbidden imports (for example `import openai`
outside `v1/model_gateway/backends/`) and fails the build on a violation.

## 7. Shared library policy

Shared code is a liability, so it is kept small and ruled tightly.

- `v1/observability`: one JSON log format, `new_trace_id`, `Timer`, and the
  request `TraceMiddleware`. No service state, no I/O beyond stdout.
- `v1/shared`: request and response contracts used across services, small pure
  helpers, and constants. No network calls, no database access, no framework
  objects beyond pydantic models.
- `v1/identity` pure helpers (`Principal`, `verify_token`, `can`): importable as
  policy, as in rule 3 above.

Anything that wants service state, a database handle or a network client is not
a shared library. It belongs to a service. A shared module never imports a
service, and a change to a shared contract is a reviewed change with every
consumer updated in the same commit.

## 8. Environment configuration pattern

- Secrets never live in source or in git. They live in root owned files under
  `/etc/nexa/*.env`, mode 600, loaded by systemd `EnvironmentFile`.
- A shared signing secret, `/etc/nexa/identity.env` with `NEXA_IDENTITY_SECRET`,
  is loaded by every service that verifies tokens.
- Each service reads its own settings from environment variables with safe
  defaults for local dev, through its own `config.py`. No service reads another
  service's env file for anything other than the shared identity secret.
- Service to service tokens (for example the runtime's gateway token) are minted
  from the Identity CLI and stored in a per service root owned env file.
- Each service ships a `.env.example` listing its variables with blank secrets.
- Deployment manifests live in `v1/deploy/` and are the source of truth for how
  each service runs. A copy of each live unit is kept there.

## 9. Migration mapping from v0

Grounded in the v0 `main.py` (5599 lines) on this repository. Each v0 capability
maps to one v1 owner. Decisions use the NEXA-M0-007 vocabulary (KEEP, IMPROVE,
REPLACE, RETIRE, NEW IN v1).

| v0 capability (evidence in main.py) | v1 owner | Decision |
|---|---|---|
| FastAPI monolith `app` and all routes (one `FastAPI()` at line 4169, ~20 routes) | split across API Gateway, Agent Runtime, Knowledge, Memory | REPLACE |
| `/chat` orchestration and chat loop (line 5016) | Agent Runtime | REPLACE |
| Intent classification and routing (intent and classify logic, ~40 sites) | Agent Runtime (agent selection), later Safety for policy routing | IMPROVE |
| Local LLM via Ollama (10 sites) | Model Gateway, ollama backend | IMPROVE |
| General knowledge path via OpenAI gpt-4o (7 sites) | Model Gateway, openai backend | IMPROVE |
| Embeddings via nomic-embed (2 sites) | Model Gateway embeddings, consumed by Knowledge | IMPROVE |
| Curriculum RAG over Chroma (15 sites) | Knowledge Service | IMPROVE |
| Vector store (Chroma persisted store) | Knowledge Service, pgvector per ADR-0002 | REPLACE |
| LMS and protected content ingestion (`/ingest-material` line 4705) | Knowledge Service ingestion | IMPROVE |
| Document extraction and OCR pipeline | Knowledge Service ingestion | IMPROVE |
| Source authority and ranking | Knowledge Service | IMPROVE |
| Provenance and citations | Knowledge Service | IMPROVE |
| Image generation (remote Qwen on Ilaibu, `/api/chat-image` line 4301) | Tool and Integration Gateway | IMPROVE |
| Mathematics engine (SymPy, ~70 sites) | Tool and Integration Gateway (math tool) | IMPROVE |
| Student and teacher role handling | Identity and Policy (roles), enforced in Agent Runtime | IMPROVE |
| Identity, authentication, RBAC (query string email and name today) | Identity and Policy, signed tokens and `can()` | REPLACE |
| Session and conversation memory, chat history (`/api/chat-history`, `/api/chat-sessions`) | Memory Service | IMPROVE |
| Share chat links (`/api/share-chat`, `/share/{token}`) | Memory Service | KEEP |
| Logging and audit data | Observability shared library and a sink | IMPROVE |
| School and core sync and retry | out of NEXA scope, owned by the EduNeX sync tooling | KEEP |
| Databases and schemas (MySQL `nexa_ai`, 11 sites) | Memory Service on PostgreSQL per ADR-0002 | REPLACE |
| Cloudflare public exposure path | API Gateway behind the tunnel | IMPROVE |
| Secrets and credentials management | environment pattern in section 8 | REPLACE |
| QA, evaluation and safety controls | Safety and Evaluation | NEW IN v1 |
| Web retrieval and language support (experimental) | Tool and Integration Gateway, behind policy | IMPROVE |
| Edge and offline capability (NEXA Mini, ADR-0007) | separate edge build, consumes the same contracts | NEW IN v1 |
| Model lifecycle and version governance | Model Gateway registry plus governance | NEW IN v1 |
| Prompt and policy governance | Agent Runtime and Safety | NEW IN v1 |
| Observability and evaluation services | Observability shared, Safety and Evaluation | NEW IN v1 |
| Tool and Connector Gateway and MCP direction | Tool and Integration Gateway (ADR-0006) | NEW IN v1 |

Outbound HTTP in v0 uses blocking `requests`. v1 services use async `httpx`
inside the service that owns the call, never from shared code.

## 10. Acceptance check against the task

- Modular: one package per service under `v1/`, one owner each. Met.
- Testable: services depend only on HTTP contracts and two ruled shared
  libraries, so each runs and tests in isolation. Met.
- Avoids direct cross service coupling: import rules in section 6, enforced by a
  CI grep. Met.

## 11. Open items

- Knowledge, Memory, Tool Gateway and Safety are boundaries here, not yet code.
  Each needs its own skeleton in a later M1 or M2 step, following this contract.
- The CI import check in section 6 is specified but not yet wired into a
  pipeline; it is a small follow up.
- The v0 freeze and removal schedule is owned by NEXA-MIG-001.
