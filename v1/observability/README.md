# NEXA Observability (M1-007)

The shared logging, tracing and audit layer for v1 (NEXA-ARCH-001 section 15).
One JSON log format and one trace id scheme across every service.

## What it provides

- `log_event(**fields)`  write one JSON object per line to stdout, with
  sensitive keys redacted.
- `audit_event(action, decision, principal, **extra)`  record a privileged
  action or policy decision (decision is allow or deny).
- `redact(obj)`  mask sensitive and student data recursively.
- `new_trace_id()`  a hex trace id.
- `Timer`  wall time in milliseconds for a call.
- `TraceMiddleware(service)`  assign a trace id per request (honouring the
  inbound `X-NEXA-Trace-Id`), log a structured `request` record, and set the id
  on the response.
- `TRACE_HEADER`  the header name, `X-NEXA-Trace-Id`.

## One sink, one id

Every service writes JSON lines to stdout, captured by journald. The proxy hops
forward `X-NEXA-Trace-Id`, so one id links the chain: API Gateway, Agent
Runtime, Model Gateway, and the Knowledge Service for embeddings. Tail all
services together:

    sudo bash v1/deploy/nexa-logs.sh

## Redaction

`log_event` runs `redact` over its fields, so secrets and student content
(emails, names, prompts, messages, queries, image data) never reach the logs,
even if a caller passes them in. Audit events carry identifiers and decisions,
not content.

## Not in this baseline, by design

Shipping logs off box (Loki, OpenSearch), metrics dashboards, sampling, and a
retention policy. Those are later steps (NEXA-DEV-001 section 15).
