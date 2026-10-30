# NEXA-M1-008: DEV, QA and UAT Environment Baseline

Status: Pattern defined; DEV live, QA and UAT pending host provisioning
Milestone: M1

## 1. Environments

| Environment | Purpose | State |
|---|---|---|
| DEV | Active development and the current live v1 services on the DGX | Live |
| QA | Integration and test, isolated data and models | To provision |
| UAT | User acceptance with EduNeX stakeholders | To provision |
| PROD | Reserved for release readiness, not in M1 | Reserved |

DEV runs today on the DGX: API Gateway 8099, Model Gateway 8100, Identity 8101,
Agent Runtime 8102, Knowledge 8103, with the v0 service on 8000. QA and UAT need
their own hosts or isolated instances, which is infrastructure the platform team
provisions; this document is the contract they follow.

## 2. Configuration and secrets separation

Each environment has its own `/etc/nexa/*.env` files with its own secrets
(NEXA-M1-006). Code is identical across environments; only the env files differ.
A service never reads another environment's files.

## 3. Reproducible deployment

- Service code is the git branch (`nexa-v1-core`); deployment is `git pull` plus
  the systemd units in `v1/deploy/`.
- Each service is a systemd unit with `EnvironmentFile` entries, `Restart=always`
  and a fixed `ExecStart`. Bringing a service up on a new host is: clone or pull,
  create the env files, copy the unit, `systemctl enable --now`.
- No manual code edits per environment. Differences are configuration only.

## 4. Health and readiness

- Every service exposes `GET /health` (liveness). The API Gateway exposes
  `GET /ready` (readiness), which checks that the upstream runtime answers and
  returns 503 when it does not. Deployment and promotion check these.

## 5. Data and vector store isolation

- Each environment has its own database and its own vector store. DEV data never
  mixes with QA or UAT. The Knowledge Service store backend and connection come
  from that environment's config (`NEXA_KNOWLEDGE_STORE` and, for pgvector later,
  its own connection string). Learner data in non-production environments must be
  synthetic or de-identified (ties to NEXA-M0-004 privacy).

## 6. Test model and runtime configuration

- QA can point the Model Gateway registry at smaller or stubbed models through
  configuration, so tests do not depend on production model capacity. The logical
  model names stay the same; only the backend mapping changes.

## 7. Access control by environment

- Access to each host and its env files is restricted to the roles that own that
  environment. PROD access is the most restricted. Service tokens are minted per
  environment and never shared across environments.

## 8. Naming and versioning standard

- Services: `nexa-<service>` (for example `nexa-api-gateway`).
- systemd units match the service name.
- Deployed version is the git short SHA, emitted by the CI version artifact
  (NEXA-M1-009) and recorded at deploy time.
- Logical model names are environment independent; the backend mapping is per
  environment.

## 9. Acceptance check

- Reproducible deploy to DEV without code changes: met (pull plus units).
- Environment data and configuration isolated: met by per-environment env files
  and stores, once QA and UAT hosts exist.

## 10. Open items (infrastructure)

- Provision QA and UAT hosts or isolated instances with their own env files,
  databases and vector stores. This is the platform team's action; the services
  and this contract are ready.
