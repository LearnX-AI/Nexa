# NEXA Agent Runtime

The governed replacement for the v0 chat loop (NEXA-ADR-0003). It hosts named
agents, verifies the caller, enforces the agent's permission, and routes all
model traffic through the Model Gateway.

## Agents are manifests (NEXA-M3-001)

Each agent is one manifest file, `agents/<id>.json`, under the contract
`nexa.agent/1.0` (`manifest.py`, JSON Schema in `schema/`). The runtime loads
and validates every manifest at start and refuses to start if one fails. Only
`active` agents, and `deprecated` ones until their sunset date, run. See
`docs/agents/NEXA-M3-001-agent-contract-lifecycle.md`.

    python -m v1.agent_runtime.manifest validate

## Request flow

1. A caller presents a signed NEXA identity token (NEXA-ADR-0005).
2. The runtime verifies it and builds a `Principal`.
3. It looks up the agent, checks its lifecycle state, and checks that the
   caller holds `required_permission` and one of the manifest's `allowed_roles`.
4. It prepends the agent system prompt and calls the Model Gateway on 8100
   with its own service token, within the manifest's timeout, retries and
   fallback models.
5. It returns the model output with an `x_nexa` trace block (agent, model,
   runtime, trace_id, latency_ms, principal).

No direct Ollama or OpenAI access: all model calls go through the gateway.

## Endpoints

- `GET  /health`  liveness and the agent list, no auth.
- `GET  /v1/agents`  list agents with state and roles, requires a valid principal.
- `GET  /v1/agents/{name}/manifest`  the agent's manifest and fingerprint.
- `POST /v1/agents/{name}/run`  run an agent, enforces the agent permission.

## Config (environment)

- `NEXA_IDENTITY_SECRET`  shared signing secret, required to verify callers.
- `NEXA_AGENT_GATEWAY_TOKEN`  service token the runtime presents to the gateway.
- `NEXA_GATEWAY_URL`  default `http://127.0.0.1:8100`.
- `NEXA_AGENT_HOST` / `NEXA_AGENT_PORT`  default `127.0.0.1:8102`.

## Not in this skeleton, by design

Tool calling and the Tool Gateway / private MCP (NEXA-ADR-0006), per agent data
scopes and RAG (NEXA-ADR-0004), multi step planning, and streaming. Those are
later phase work. Do not add future phase work here (NEXA-DEV-001 section 15).
