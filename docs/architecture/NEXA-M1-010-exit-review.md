# NEXA-M1-010: Core Platform Integration Test and M1 Exit Review

Status: Draft for governance decision
Milestone: M1 (exit record)

## 1. Purpose

Validate the M1 core platform end to end before M2, record the test evidence,
the open defects and risks, any deviations needing an ADR, and an M1 acceptance
recommendation. M2 must not start until critical M1 defects are closed or
explicitly accepted through governance.

## 2. Validation checklist and evidence

Evidence is from DEV on the DGX with the five services live (8099 to 8103) and
the Observability layer in place.

| Check | Result | Evidence |
|---|---|---|
| API Gateway receives an authenticated request | PASS | teacher token to 8099 `/v1/agents` and `/v1/agents/nexa-tutor/run` returned 200 |
| Identity and policy decision enforced | PASS | auditor denied run with 403, invalid token 401, no token 401; audit events emitted |
| Agent registry resolves a test agent | PASS | nexa-tutor and nexa-precise registered and listed with version and capabilities |
| Agent invokes the Model Gateway | PASS | tutor run reached the gateway with the service token |
| Model adapter returns a response | PASS | Ollama llama3.1 returned an answer through the gateway |
| Correlation id and telemetry visible end to end | PASS | one X-NEXA-Trace-Id propagates API Gateway to runtime to gateway; JSON logs in journald |
| Secrets remain externalized | PASS | all secrets in root-owned `/etc/nexa/*.env`; startup preflight refuses missing secrets; logs redacted |
| Knowledge retrieval works through the gateway | PASS | ingest then query returned the chunk with a cosine score; RBAC 403 for auditor ingest |
| DEV deployment healthy | PASS | each `/health` ok, API Gateway `/ready` 200 |
| QA deployment healthy | NOT YET | QA host not provisioned (NEXA-M1-008) |
| Failure path and rollback demonstrated | PARTIAL | failure paths return 502 and are handled; live rollback not yet exercised (NEXA-M1-009) |

## 3. Services at M1

| Service | Port | ADR | State |
|---|---|---|---|
| API Gateway | 8099 | ARCH-001, M1-002 | Live, /health and /ready, central errors, correlation ids |
| Model Gateway | 8100 | ADR-0001, M1-003 | Live, Ollama and OpenAI adapters, retries, vLLM and NIM stubs |
| Identity and Policy | 8101 | ADR-0005, M1-005 | Live, signed tokens, RBAC with baseline roles, SSO seam |
| Agent Runtime | 8102 | ADR-0003, M1-004 | Live, registry with version and capability fields, trace propagation |
| Knowledge | 8103 | ADR-0004 | Live, embeddings via gateway, in-memory store, pgvector deferred |
| Observability | n/a | M1-007 | Shared library wired across all services |

## 4. Open defects and risks

- QA and UAT environments not provisioned (NEXA-M1-008). Infrastructure action.
- Live rollback not yet exercised (NEXA-M1-009). Run once a QA host exists.
- Dependency and security scan gate (`pip-audit`) is wired in CI with pinned
  requirements; the first green run on GitHub is still to be recorded
  (NEXA-M0-003, NEXA-M1-009).
- EduNeX or IdP integration is a seam only; real SSO token validation is
  outstanding (NEXA-M1-005). Tokens are issued by the identity service for now.
- Knowledge persistence is in-memory; pgvector and the full ingestion pipeline
  are deferred (ADR-0002, ADR-0004).
- Memory Service, Tool and Integration Gateway and Safety and Evaluation are not
  built yet. They are defined boundaries (NEXA-M1-001), not blockers for the
  core platform, but they are required for feature parity with v0.
- Carry-over from M0: the `user_memory.json` learner-data exposure is still open
  (NEXA-M0-007). It blocks the M0 formal sign-off and should be cleared.

## 5. Deviations requiring an ADR

- None that contradict the accepted ADRs. The in-memory Knowledge store and the
  vLLM and NIM stubs are explicit deferrals within ADR-0001, ADR-0002 and
  ADR-0004, not deviations. ADR-0006 (Tool and MCP) remains Proposed and will be
  resolved when the Tool Gateway is built.

## 6. M1 acceptance recommendation

Recommendation: M1 CONDITIONAL. The core platform is validated end to end in DEV
(authenticated entry, policy enforcement, agent resolution, model invocation,
knowledge retrieval, correlation and telemetry, externalized secrets). Promote
to M2 conditional on closing or formally accepting these items:

1. Provision QA (and UAT) and demonstrate a healthy deployment and a live
   rollback there.
2. Wire the dependency and security scan gate after pinning dependencies.
3. Decide the EduNeX SSO integration plan (seam to real validation).
4. Contain the M0 `user_memory.json` exposure.

The remaining service skeletons (Memory, Tool Gateway, Safety and Evaluation)
are tracked for delivery and are not core-platform blockers. Final approval to
enter M2 is a governance decision and is not inferred from task completion.
