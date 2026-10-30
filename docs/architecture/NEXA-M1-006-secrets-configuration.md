# NEXA-M1-006: Secrets, Configuration and Environment Management

Status: Implemented (code) with an operations procedure
Milestone: M1
Applies to: DEV, QA, UAT, PROD

## 1. Principle

No environment-specific values and no credentials in source, repositories, ZIPs,
task trackers or screenshots. Secrets live only in root-owned environment files
on the host and are loaded at runtime. Non-secret defaults live in code. The
four environments differ only by their environment files.

## 2. Configuration schema

Each service reads its settings from environment variables through its own
`config.py`, with safe dev defaults for non-secrets and no default for secrets.

| Variable | Secret | Owner | Notes |
|---|---|---|---|
| NEXA_IDENTITY_SECRET | yes | shared | HS256 signing secret, loaded by every service |
| NEXA_AGENT_GATEWAY_TOKEN | yes | agent runtime | service token to call the gateway |
| NEXA_KNOWLEDGE_GATEWAY_TOKEN | yes | knowledge | service token to call the gateway |
| OPENAI_API_KEY | yes | model gateway | general knowledge model |
| NEXA_DB_PASSWORD | yes | data services | database password, no default |
| NEXA_GATEWAY_URL, NEXA_RUNTIME_URL | no | per service | internal URLs, dev defaults |
| NEXA_*_HOST, NEXA_*_PORT | no | per service | bind addresses, dev defaults |
| NEXA_EMBED_MODEL, NEXA_KNOWLEDGE_STORE | no | knowledge | logical model, store backend |
| NEXA_GATEWAY_RETRY_ATTEMPTS, NEXA_GATEWAY_RETRY_BACKOFF | no | model gateway | retry policy |

## 3. Secret storage

- Secrets are stored in root-owned files under `/etc/nexa/*.env`, mode 600,
  owner root. Current files: `nexa.env` (DB password, OpenAI key, ingest token),
  `identity.env` (the shared signing secret), `model-gateway.env`,
  `agent-runtime.env`, `knowledge.env`.
- systemd loads them with `EnvironmentFile=` so the process sees them as
  environment variables. The unit files are versioned under `v1/deploy/`; the
  secret files are not in git.

## 4. Loading pattern and startup validation

- Each service has a `config.py` that reads variables with `os.getenv` for
  non-secrets (with defaults) and expects secrets from the environment.
- At startup every service calls `v1.shared.config.preflight(service, required)`.
  If any required variable is missing or blank, `preflight` raises `ConfigError`
  and the service refuses to start. This is wired into all five services and is
  covered by the test suite and the CI config validation gate.

## 5. Redaction rules

- All logging goes through `v1.observability.log_event`, which runs `redact`
  over its fields. Secret keys (authorization, token, password, api_key, secret)
  and student or user content (email, name, prompt, message, query, text, image
  data) are masked before any line is written. Audit events carry identifiers
  and decisions, never content.

## 6. Rotation procedure

1. Generate the new secret (for example `openssl rand -hex 32` for the signing
   secret).
2. Write it to the service's root-owned env file under `/etc/nexa`.
3. Restart the affected services. For the shared signing secret, restart all
   services together, then mint fresh service tokens with the identity CLI and
   update the per-service token env files.
4. For the database password, rotate in the database first (ALTER USER), then
   update `nexa.env` and restart.
5. Record the rotation date in the operations log. Do not record the secret.

## 7. Separation of non-secret defaults from secrets

Non-secret defaults are committed in each `config.py` and in the `.env.example`
files. Secrets appear only as blank keys in `.env.example`. A real value for a
secret never appears in the repository.

## 8. Acceptance check

- Starts in DEV and QA from externalized configuration: yes, via the env files.
- Refuses to start on a missing required secret: yes, `preflight` raises and the
  CI gate proves the check. 
- No secret material committed or attached: enforced by the pattern, the
  `.gitignore` of `*.env`, and redaction in logs.

## 9. Open items

- Per-environment env files for QA, UAT and PROD are provisioned with the
  environments (NEXA-M1-008); the schema here is the contract they follow.
- A managed secret store (for example Vault or a cloud secret manager) can
  replace the root-owned files later without changing the loading pattern.
