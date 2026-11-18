# NEXA-M3-006 — Agent State, Session & Memory Handoff

Status: implemented (this change). Owner: NEXA AI engineering.

Builds on:

- M3-003 (orchestration);
- M3-005 (messages, idempotency).

Code:

- the contract: `v1/agent_runtime/state/contract.py`;
- the store and its policy: `state/store.py`;
- retention: `state/purge.py`;
- the API: `state/api.py`.

## Three kinds of state, kept apart

| | What | Lives | Who |
|---|---|---|---|
| **Run state** | The working state of one workflow run: step outputs, routing and review decisions, counters. | The run, then 30 days (`NEXA_STATE_RETENTION_DAYS`). | The engine writes it; the run's viewers can see checkpoint metadata. |
| **Session state** | A small key/value record one user owns, to hand context from one run to the next (for example, the last topic prepared). | Expires after `ttl_s` idle (default 24 h, at most 7 days), and never lives beyond 30 days. | Only the owner, and only through workflows the owner named. |
| **Learner records and long-term memory** | Marks, progress, profiles. | **Not here.** They belong in the LMS and the Memory Service (port 8104, not built yet). | — |

State may carry a **memory reference** to long-term data, never a copy:
`{"$memory": {"service", "scope", "subject", "key", "version"}}`. A
reference is accepted only for scopes the agent's manifest declares in
`memory.long_term_scopes`. Today no agent declares any, so none are
accepted.

## Serialisation: `nexa.state/1.0`

Every record has the same shape:

- schema, kind (`session` or `run_checkpoint`), id;
- owner (subject and roles);
- scope:
  - a session: its workflows, ttl and maximum age;
  - a checkpoint: the workflow, version, fingerprint, step and status;
- a **version** that increases on every write;
- timestamps and expiry;
- the data and its **sha256 digest**.

The schema is published in `v1/agent_runtime/schema/nexa-state-1.0.schema.json`
and checked in CI.

**Rules on content** (`check_data`):

- **Shape and size.** JSON values only. Session state is at most 6 levels
  deep and 32 KB; run state at most 16 levels and 512 KB. Strings are at
  most 8,000 characters.
- **No credentials.** Nothing that looks like a token or a bearer header.
  The user's token is never written to state, so a run that needs it after a
  pause asks the user for their session again.
- **Session state only:**
  - no learner-record fields (marks, grades, scores, attendance, date of
    birth, address, phone, guardian, ...);
  - no personal data (email addresses, phone numbers).

**Writes** say which version they expect. A stale write is a 409 conflict,
never a silent overwrite. A record whose digest does not match its data is
never used.

## Context boundaries

- **Agents never see state.** They see only the prompt the engine renders
  for that step.
- **Templates only reach what the workflow declares:**
  - its inputs and earlier steps;
  - session keys it declares in `session.read`, as a snapshot taken when
    the run starts.
- **Session writes are declared too.** A workflow writes only the keys in
  `session.write`, and only when the run succeeds (the handoff). If another
  run wrote in between, the workflow rereads and writes its own declared
  keys on the new version; the other keys stand.
- **A session is usable only by the workflows its owner named.** It is not
  visible to any other user.

## Workflow checkpoints and recovery

- **Checkpoints.** The engine writes a versioned, digested checkpoint when
  the run starts and after every step: context, current step, counters,
  status. The last 5 are kept while the run lives.
- **After a restart.** Runs that were executing become `interrupted` and
  show `resumable: true` when:
  - a valid checkpoint exists, **and**
  - the workflow's fingerprint is unchanged. A run never resumes on a
    different definition.
- **Resume** (`POST /v1/workflow-runs/{id}/resume`):
  - only the person who started the run may resume it, with their own
    session;
  - the run continues from the newest valid checkpoint;
  - the step that was executing runs again. Work it had already finished is
    **not redone**, because every agent and tool request repeats its
    idempotency key (M3-005): the bus answers it from the recorded result,
    and the conversation shows those requests as `duplicate`.
- **Runs paused at a human checkpoint** are already durable. They wait in
  the database and resume when someone decides.

## Policy and audit

| Action | Allowed |
|---|---|
| Read or write session values | the owner, directly or through a named workflow, declared keys only |
| Session metadata (keys, size, version, expiry; never values) | the owner, admin, platform_admin |
| Delete a session | the owner, admin, platform_admin |
| Resume a run | its initiator |
| Read checkpoints (metadata) | the run's viewers |
| Read the state access log | auditor, admin, platform_admin, ai_engineer |

Every state access, allowed or refused, is one row in `state_access` and one
audit event. A row holds:

- time, actor and roles;
- the action: create, read, write, metadata, delete, resume or purge;
- the kind and target;
- version, keys, the workflow it came through, decision, reason and trace
  id.

Read it with `GET /v1/state/access?target=&actor=`.

## Retention

Retention runs in the Agent Runtime at start and then every hour. It can
also be run by hand with `python -m v1.tools.state_retention [--dry-run]`.

It removes:

- expired sessions;
- for runs that ended more than the retention period ago:
  - their checkpoints;
  - their input, context and output;
  - their idempotency keys.

The run's status, counters, trace and message log stay as the audit record.
Each purge is logged as `system:state-retention`.

## API

- `POST /v1/sessions` `{"workflows": [...], "ttl_s": 86400}`
- `GET /v1/sessions/{id}`: the owner gets the record; admins get
  metadata only.
- `PUT /v1/sessions/{id}/state` `{"data": {...}, "expected_version": n}`
- `DELETE /v1/sessions/{id}`
- `POST /v1/workflows/{id}/runs` with `"session_id"`
- `POST /v1/workflow-runs/{id}/resume`
- `GET /v1/workflow-runs/{id}/checkpoints`
- `GET /v1/state/access`, `GET /v1/state/schema`

The API Gateway forwards all of them.

`nexa-wf-lesson-explainer` 1.1.0 uses a session:

- it reads `last_topic` and `last_grade`;
- on success it writes the topic, the grade and who reviewed it;
- the next run's coordinator is told the earlier topic.

## Acceptance

| Criterion | Evidence |
|---|---|
| Multi-step workflows resume safely | `test_interrupted_multi_agent_run_resumes_without_redoing_work`: a crash during the second coordinator call; after a restart the run is interrupted and resumable; only its initiator may resume it; the 2 finished calls are answered from the idempotency record; the run completes and the conversation is valid. `test_resume_refused_on_a_changed_workflow_or_a_damaged_checkpoint`. Live: `python -m v1.tools.state_check` restarts the DGX Agent Runtime in the middle of a run and resumes it. |
| State access is policy-controlled | `test_session_policy`: other users see nothing; admins metadata only; undeclared workflows refused; learner records, personal data, credentials and undeclared memory scopes refused; stale writes conflict; expired sessions refused and purged |
| Auditable | Every test above checks `state_access`; `test_api_through_the_gateway` (auditor reads the log) |
| Session handoff | `test_session_hands_context_from_one_run_to_the_next` |
| Checkpoints and serialisation | `test_every_step_is_checkpointed_without_credentials`, `test_published_state_schema` |
| Retention | `test_retention_purges_state_but_keeps_the_audit_record` |
| Memory references | `test_memory_references_are_allowed_only_for_declared_scopes` |

## Not in this change

- **The Memory Service** (long-term memory, learner profiles) is not built.
  The reference format and its policy are in place for it.
- **Message log retention.** Message payloads (M3-005), including prompts,
  are kept as the audit record. Purging them follows the audit retention
  policy, which is still to be decided (NEXA-GOV-001).
- **Resuming knowledge and tool steps** needs the user's own session, so it
  is the initiator who resumes. This is by design.
