# NEXA-M3-005 — Inter-Agent Messaging & Event Contract

Status: implemented (this change). Owner: NEXA AI engineering.

Builds on:

- ADR-0003 (Agent Runtime);
- M3-003 (orchestration: agents never call agents);
- M3-004 (Tool Gateway).

## Principle

Agents never converse with each other. Every exchange that involves an agent
is a message between that agent and the **orchestrator**, carried by the
**message bus**. The bus validates each message, routes it, records it and
deduplicates it.

When several agents work together, the orchestrator is in the middle:

1. A coordinator agent's **result** proposes the next action.
2. The orchestrator decides, recorded as a `delegation.choice` **event**.
3. The orchestrator sends a new **request** to the chosen agent.

The coordinator never addresses the other agent.

Code:

- the contract: `v1/agent_runtime/messaging/contract.py`;
- the bus: `v1/agent_runtime/messaging/bus.py`;
- the published schema, the envelope and every payload version:
  `v1/agent_runtime/schema/nexa-message-1.0.schema.json`. CI fails if it
  drifts from the code.

## Message kinds

| Kind | Meaning | Sent by → to |
|---|---|---|
| `request` | Do one piece of work. | orchestrator → one agent, or one tool on an agent's behalf |
| `result` | The answer to a request (`in_reply_to`). | the request's receiver → the orchestrator |
| `error` | It could not be done: code, retryable, reason. | the request's receiver → the orchestrator |
| `event` | Something happened; it is never answered. | orchestrator, runtime or a service → the run's event log |

## Envelope: `nexa.message/1.0`

| Field | Rule |
|---|---|
| `schema` | `nexa.message/1.0`. A change to the envelope is a new version. |
| `message_id` | `msg_…`, unique. |
| `kind`, `type` | Kind as above. `type` comes from a closed list (`agent.task`, `agent.result`, `agent.error`, `tool.task`, `tool.result`, `tool.error`, `delegation.choice`), or for events from a named family (`run.*`, `step.*`, `route.*`, `checkpoint.*`, …). A type is never reused for another meaning. |
| `payload_schema` | The payload's own version, for example `nexa.payload.agent.task/1`. It is fixed per type and validated. |
| `correlation_id` | The conversation: every message of a workflow run carries the run id. |
| `causation_id` | The message that caused this one. Every chain leads back to the run's `run.started` event. |
| `in_reply_to` | Required on results and errors: the request being answered. |
| `trace_id` | The distributed trace, shared with every service call (Model Gateway, Tool Gateway, Knowledge Service). |
| `sender`, `receiver` | `{kind, id, version}`, where kind is orchestrator, agent, tool, user or service. Events have no receiver. |
| `principal` | The user the work is for: subject and roles. |
| `permission` | The permission the receiver checks for that user. |
| `deadline`, `timeout_s` | Requests only. A request is not started after its deadline, and a result after it is discarded. |
| `attempt`, `max_attempts` | Requests only. A retry is a **new** request with `attempt + 1` and the same idempotency key. |
| `idempotency_key` | Requests only. The same key with the same payload is done once; its recorded result is returned to any repeat. The same key with a different payload is a `conflict`. |
| `payload` / `error` | The content, validated against `payload_schema`. Error messages carry `error` and no payload. |

The payload schemas are versioned separately (`/1`):

- `agent.task`: role (agent, coordinator, delegate), workflow, step,
  prompt, model settings;
- `agent.result`: output, agent version, manifest fingerprint, model,
  attempts, latency;
- `tool.task` and `tool.result`: tool, the agent it is for, arguments,
  scopes; and the call id, tool version, scopes and result;
- `delegation.choice`: what the coordinator proposed, whether it was
  accepted, the action and the delegate;
- `workflow.event`: workflow, run, step, sequence number and detail.

## Routing rules (the bus refuses anything else)

- Only the orchestrator sends requests, and only the orchestrator addresses
  an agent.
- A result or error comes only from the request's receiver, and goes back to
  the request's sender.
- Agents never send events and never message agents.

## Permissions

- **Each request names the user and the permission.** The receiver checks
  them again:
  - the Agent Runtime checks the agent's lifecycle, kill switch, roles and
    run permission;
  - the Tool Gateway checks its allowlist, scopes and policy.
- **No message can raise its own authority.** The principal is the user who
  started the run, never an agent.

## Timeouts, retries, idempotency and failure semantics

| Code | Meaning | Retryable |
|---|---|---|
| `timeout` | No result before the deadline. | yes |
| `unavailable` | The receiver or its dependency is down (502/503). | yes |
| `refused` | Not permitted, disabled, not active, or not granted (401/403/409/410/423). | no |
| `invalid` | The request or its arguments are wrong (404/422). | no |
| `conflict` | Idempotency key reused for different work. | no |
| `budget` | Rate limit, per-run cap or run budget exhausted (429). | no |
| `cancelled` | The run stopped. | no |
| `internal` | A bug. It is recorded and never retried blindly. | no |

- **Retries.** Retryable errors are retried by the orchestrator only:
  - with backoff;
  - up to the step's `max_attempts`;
  - under the same idempotency key.

  Separately, the Agent Runtime retries the model call according to the
  agent's manifest (`runtime.retry`), and that retry is invisible at the
  message level.
- **Delivery** is at least once. Idempotency keys make every piece of work
  happen at most once, so a run sees each effect exactly once.
- **Idempotent starts.** `POST /v1/workflows/{id}/runs` with an
  `Idempotency-Key` header returns the same run for a repeat of the same
  input, and 409 for different input. The API Gateway forwards the header.
- **Late results.** A result that arrives after its request's deadline is
  recorded with status `late`, and the orchestrator treats the request as
  timed out.

## Where the messages are

The bus records every envelope of a run, in order, in the registry database
(`messages` and `message_idempotency`). Each workflow trace entry
(M3-003) is also published as a `workflow.event` event.

- `GET /v1/workflow-runs/{run_id}/messages` returns the whole conversation,
  with a server-side check against the contract. The same people can see it
  as can see the run.
- `GET /v1/messaging/schema` returns the published schema.

## Acceptance

| Criterion | Evidence |
|---|---|
| Versioned message and event schemas, documented | This document; `nexa-message-1.0.schema.json` (envelope plus payloads `/1`, error codes); CI check; `test_schema_version_is_fixed_and_published` |
| Validated in a multi-agent workflow | `test_multi_agent_conversation_follows_the_contract`: nexa-wf-lesson-explainer, with coordinator → orchestrator decision → delegates and a human checkpoint. Every envelope and payload validates; one correlation and trace id; replies come from receivers; causation chains; no agent-to-agent message. Live: `python -m v1.tools.messaging_check` revalidates a DGX run independently. |
| Correlation, trace, sender and receiver identity | The same test |
| Permissions | Every request carries principal and permission (same test); refusals are final (`test_refused_is_final_no_retry`) |
| Timeout, retry | `test_deadlines`, `test_retry_is_a_new_attempt_under_the_same_idempotency_key` |
| Idempotency | `test_idempotency_done_once_and_conflicts_refused`, `test_api_messages_and_idempotent_start`, `test_api_gateway_forwards_the_idempotency_key` |
| No uncontrolled peer-to-peer conversations | `test_envelope_rules` (an agent cannot send a request, message an agent, or emit an event); runtime guard from M3-003 |

## Not in this change

- **The bus is in-process.** It runs inside the Agent Runtime and keeps its
  log in SQLite. The envelope does not depend on that, so moving to a broker
  later (for example, to reach school servers) changes how messages are
  carried, not the contract.
- **Direct agent calls.** `POST /v1/agents/{name}/run` (a user calling one
  agent directly) is not wrapped in envelopes. It is not an inter-agent
  exchange, and it is already traced and audited.
