# NEXA Model Gateway (M1 skeleton)

One OpenAI-compatible entry to every approved model. Callers use logical model
names; the gateway resolves them to a backend and version, applies the identity
and policy hook, and stamps model, version, runtime and a trace id into every
response. This is the first M1 service (NEXA-ADR-0001). NEXA v0 is not changed.

## What it does now

- Fronts the existing Ollama models and OpenAI gpt-4o. No behaviour change to v0.
- Logical models: `nexa-chat-default`, `nexa-chat-precise`, `nexa-general`,
  `nexa-embed-default` (see `registry.py`).
- Endpoints: `POST /v1/chat/completions`, `POST /v1/embeddings`,
  `GET /v1/models`, `GET /health`.
- Requires a bearer token (`NEXA_GATEWAY_TOKEN`), so model access is not open.
- Every response carries an `x_nexa` block (model, version, runtime, trace id)
  and an `X-NEXA-Trace-Id` header.

## Run

```
pip install -r v1/model_gateway/requirements.txt
export NEXA_GATEWAY_TOKEN=some-strong-token
export OPENAI_API_KEY=...            # only needed for nexa-general
uvicorn v1.model_gateway.main:app --host 127.0.0.1 --port 8100
```

## Try it

```
curl -s http://127.0.0.1:8100/health

curl -s http://127.0.0.1:8100/v1/models \
  -H "Authorization: Bearer $NEXA_GATEWAY_TOKEN"

curl -s http://127.0.0.1:8100/v1/chat/completions \
  -H "Authorization: Bearer $NEXA_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"nexa-chat-default","messages":[{"role":"user","content":"Say hello in one short sentence."}]}'
```

## Not in this skeleton, by design

Streaming responses, vLLM and NVIDIA NIM backends, fallback routing, rate
limiting, and real identity and RBAC. These are the next steps of M1 and of
ADR-0001 and ADR-0005. Do not add future-phase work here (NEXA-DEV-001
section 15).

## Design notes

- Adding a runtime means adding one backend module that implements
  `backends/base.py`, then one registry entry. Callers do not change.
- The `Principal` from `identity.py` is the seam where authenticated identity
  and RBAC attach later, so endpoints that depend on it stay the same.
- Secrets come from the environment and are never logged.
