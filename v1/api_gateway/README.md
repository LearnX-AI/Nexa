# NEXA API Gateway (M1 skeleton)

The single external entry for v1 (NEXA-ARCH-001). The frontend and the Moodle
plugins target this service, not the internal services directly. It verifies the
caller's identity token at the edge, applies the CORS policy, and proxies to the
Agent Runtime.

## Request flow

1. A caller presents a signed NEXA identity token (NEXA-ADR-0005).
2. The gateway verifies the signature and claims. Invalid or missing tokens are
   rejected with 401 before anything is proxied.
3. The request is forwarded to the Agent Runtime with the caller's token intact.
4. The runtime applies per agent RBAC and returns the result, which the gateway
   passes back with the upstream status code.

The gateway holds no service token and makes no model calls of its own.

## Endpoints

- `GET  /health`  liveness, no auth.
- `GET  /v1/agents`  list agents, requires a valid token.
- `POST /v1/agents/{name}/run`  run an agent, requires a valid token.

## Config (environment)

- `NEXA_IDENTITY_SECRET`  shared signing secret, required to verify callers.
- `NEXA_RUNTIME_URL`  upstream runtime, default `http://127.0.0.1:8102`.
- `NEXA_API_HOST` / `NEXA_API_PORT`  default `127.0.0.1:8099`.
- `NEXA_CORS_REGEX`  allowed browser origins, default EduNeX over https.

## Not in this skeleton, by design

Rate limiting, request quotas, API keys for server to server callers, response
streaming, and the public tunnel wiring that makes this the internet facing
entry. Those are later steps. Do not add future phase work here
(NEXA-DEV-001 section 15).
