# NEXA-M3-008 — Agent Observability, Tracing & Audit

Status: implemented (this change). Owner: NEXA AI engineering.

It builds on the M1-007 observability baseline (`v1/observability`: JSON logs,
trace ids, redaction, metrics). It also builds on what each M3 layer already
records:

- M3-003: the workflow trace;
- M3-004: the Tool Gateway audit;
- M3-005: messages;
- M3-006: state access;
- M3-007: policy decisions.

Code:

- `v1/agent_runtime/tracing/`: `signals.py`, `reconstruct.py`, `api.py`;
- tools: `v1/tools/trace_view.py` (the operator CLI) and
  `v1/tools/observability_check.py` (the live check);
- tests: `v1/tests/test_observability.py`.

## One id across the whole workflow

| Id | What it identifies | Where it goes |
|---|---|---|
| **trace id** (`X-NEXA-Trace-Id`) | One request chain. | Minted at the API Gateway, or honoured from the caller. Forwarded to the Agent Runtime, the Model Gateway, the Tool Gateway and the Knowledge Service. On every log line, message, policy decision and Tool Gateway audit record. |
| **correlation id** | One workflow run (`wfr_…`). | On every nexa.message/1.0 envelope (M3-005), and in every decision, tool call and state access as `run_id`. |
| **span id** | One call. | A request's `message_id`. The step span is `step:<seq>`; the run span is `run:<id>`. |
| **causation id** | What caused each message. | Every chain leads back to `run.started`. |
| **call id** (`tc_…`) | One tool call, as the Tool Gateway saw it. | In the tool result, and joined in the reconstruction. |

## What is captured

| Signal | Where it is recorded |
|---|---|
| Request and response | Each agent or tool request, and its result or error, is a message. Content is kept in the store; operators see its size and digest. |
| Model calls | Each agent call's result (payload `nexa.payload.agent.result/2`, new here): model, fallback model, attempts, prompt and completion tokens, latency. `/1` stays valid for older records. |
| Tool calls | Message pairs, joined with the Tool Gateway's audit record by call id: decision, deciding check, status, latency, implementation, connector, scopes, argument digest. |
| Retrieval references | The curriculum passages a tool cited: marker, document title, page, chunk id, used or not, plus evidence status and confidence. These are curriculum metadata, not learner data. |
| Latency | Per step, per call (request to reply), per run (wall time and running time); per call in the span logs. |
| Tokens and cost | Per call and summed per model. Cost uses `NEXA_MODEL_COSTS`, prices per 1,000 tokens per logical model. Local models have no per-token charge, so cost reads "not configured" until prices are set. |
| Policy decisions | Every allow and deny, with the rule, placed in the step where it happened. |
| Errors and retries | Error code, retryable flag, status, attempts; step failures; error paths; fallbacks; work answered from the idempotency record (a resumed run). |
| Handoffs | Coordinator result → orchestrator decision → delegate request, as linked message ids. |
| Human decisions | Waiting, refused (role, four eyes, school), decided (who, pseudonymised). |
| State access | Checkpoint and session reads and writes made by the run. |

**Metrics** are Prometheus counters on each service's `GET /metrics`. Their
labels are low-cardinality: agents, models, tools, rules, statuses. They
never carry a user, an id or content.

- `nexa_agent_calls_total{agent,role,status}`
- `nexa_agent_call_ms_sum` and `_count{agent,role}`
- `nexa_model_tokens_total{model,kind}`
- `nexa_tool_calls_total{tool,status}`
- `nexa_tool_call_ms_sum` and `_count{tool}`
- `nexa_policy_decisions_total{action,rule,decision}`
- `nexa_workflow_runs_total{workflow,status}`

**Span logs.** Each agent call, tool call and finished run writes one JSON
log line (`event: span`) with the trace id, run id, span id, parent, agent or
tool, status, latency and tokens. They go to journald next to every other
service's lines under the same trace id (`v1/deploy/nexa-logs.sh`).

## Reconstructing an execution

The reconstruction is `GET /v1/observability/runs/{run_id}`, or
`/v1/observability/traces/{trace_id}` for every run under a trace. The
operator CLI is
`python -m v1.tools.trace_view <run or trace id> --operator user:<name>`.

The record, `nexa.trace/1.0`, is assembled from the records above; nothing
extra is stored. It has:

- a **summary**: status, duration, steps, agent calls and agents, model
  usage, cost, tool calls, retrieval references, handoffs, retries, work
  answered from idempotency, errors, policy allow and deny counts;
- the **tree**: run → steps → agent and tool calls and events, in time order;
- the **handoffs**, the **policy decisions** and the **state access**.

## Privacy: secrets and learner data stay out

| | Operational view (default) | Forensic view |
|---|---|---|
| Who | ai_engineer, admin, platform_admin, auditor | auditor, admin, platform_admin, **with a reason** (≥ 10 characters) |
| Users | Pseudonymised, for example `user#139819b42f`: a keyed hash, stable within the deployment, not reversible without its secret. Service and agent identities stay in clear. | Real account ids |
| Prompts, answers, tool arguments and results, inputs, outputs, review comments | Size and digest only | In full |
| Curriculum references, agents, versions, models, tokens, timings, decisions, errors | Shown | Shown |
| Email addresses, phone numbers in any remaining text | Removed | Kept |
| Anything that looks like a credential | Removed | Removed |
| Use | Logged in the state access log (`trace`) | Logged (`forensic`), with the reason |

**Secrets never reach these records.**

- User tokens are never stored (M3-006), and delegated tokens live 2 minutes
  and are never written down (M3-007).
- Service credentials stay in the Tool Gateway (M3-004).
- The reconstruction also scrubs anything token-shaped. The live check scans
  the record for the identity secret and the runtime's service token, and
  for the run's own question, topic and account names.

The services' structured audit lines (journald) keep the acting account as
`principal`, so that actions remain attributable, but no content. Those logs
are readable on the host only.

## Acceptance: operators can reconstruct a multi-agent execution without exposing secrets or unnecessary learner data

| | Evidence |
|---|---|
| Reconstruct a multi-agent execution | `test_operators_reconstruct_a_multi_agent_execution`: lesson explainer, 3 steps, 4 agent calls with versions, manifests, models, tokens and latency, 2 handoffs linked end to end, the refused reviewer and the approval, delegation and tenancy decisions, one trace id |
| Tool calls and retrieval references | `test_tool_calls_are_joined_with_the_tool_gateway_record` |
| Errors, retries, denials | `test_errors_retries_and_denials_are_reconstructed` |
| Interrupted and resumed runs | `test_an_interrupted_and_resumed_run_shows_both_parts` |
| No secrets or unnecessary learner data | `test_the_operational_view_exposes_no_secrets_or_learner_data` (topic, personal data, account names, comments, service token, any token: none); `test_metrics_and_span_logs_carry_no_content_or_identity` |
| Content only for auditors, with a reason, logged | `test_forensic_view_is_for_auditors_with_a_reason_and_is_logged` |
| Correlation and trace ids across the workflow | the tests above, `test_trace_lookup_and_text_view` |
| Degrades honestly | `test_without_the_tool_gateway_audit_the_record_says_so` |
| Live | `python -m v1.tools.observability_check` on the DGX (11 checks), also run in process in `test_live_check_passes_against_the_real_apps` |

Also in this change, from M3-006: runs that were interrupted and never
resumed are now purged after the retention period, like finished runs.

## Not in this change

- **Shipping logs off the host** (Loki, OpenSearch), dashboards and
  alerting. The signals are ready for them, but they are an operations
  decision (NEXA-DEV-001 section 15).
- **Model Gateway internal timing.** The Model Gateway's own latency per
  call is in its log lines. The reconstruction uses the runtime's measured
  latency per agent call.
- **Retention of the message log**, which holds content for forensic
  review. This depends on the audit retention policy (NEXA-GOV-001).
