# NEXA Agent Runtime (M1 skeleton)

The governed replacement for the v0 chat loop (NEXA-ADR-0003). It hosts named
agents, verifies the caller, enforces the agent's permission, and routes all
model traffic through the Model Gateway.

## Request flow

1. A caller presents a signed NEXA identity token (NEXA-ADR-0005).
2. The runtime verifies it and builds a `Principal`.
3. It looks up the agent and checks `can(principal, agent.required_permission)`.
4. It prepends the agent system prompt and calls the Model Gateway on 8100
   with its own service token.
5. It returns the model output with an `x_nexa` trace block (agent, model,
   runtime, trace_id, latency_ms, principal).

No direct Ollama or OpenAI access: all model calls go through the gateway.

## Endpoints

- `GET  /health`  liveness and the agent list, no auth.
- `GET  /v1/agents`  list agents, requires a valid principal.
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
