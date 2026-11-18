# NEXA-M3-003 — Orchestration Engine & Workflow Patterns

Status: implemented (this change). Owner: NEXA AI engineering.

Governing decision: **NEXA-ADR-0003 (Agent Runtime)**. It replaces the v0
monolith chat loop with a governed runtime boundary.

Builds on:

- NEXA-M3-001: the agent contract and lifecycle;
- NEXA-M3-002: the agent registry and discovery.

## Principle

The engine owns control; agents only return text.

- **Workflows are reviewed files.** A workflow is a file under
  `v1/agent_runtime/workflows/`, in the `nexa.workflow/1.0` contract. Its
  steps, order, limits and allowed agents are fixed in that file.
- **The engine decides every next step.** It calls each agent through the
  Agent Runtime, so every call goes through the same checks as a direct run:
  - the registry, lifecycle and kill switch;
  - the user's role and permission;
  - the agent's own timeout, retries and model fallbacks.
- **Agents never call agents.** The runtime refuses any agent run started
  while another is in progress (`runtime.active_run`), with 409 "agents are
  composed only by the orchestration engine". The same rule refuses a
  workflow started from inside an agent run.
- **Multi-agent work is bounded.** It exists only as a `delegate` step:
  - a coordinating agent *proposes* the next action;
  - the engine accepts the proposal only if it is a JSON choice from a fixed
    allowlist, and only up to a fixed number of delegations;
  - anything else is refused and traced, never guessed.
- **Runs belong to the user.** Every run executes as the user who started it,
  never as a service identity.

## The workflow contract (`nexa.workflow/1.0`)

The code is in `orchestration/workflow.py`. The published schema is
`schema/nexa-workflow-1.0.schema.json`, and CI checks both.

| Step | What it does |
|---|---|
| `agent` | Runs one agent, named or found by capability through the registry (discovery). Each step has a prompt template, a timeout per attempt, retries with backoff, fallback agents, and an `on_error` path. |
| `knowledge` | A grounded answer from the Knowledge Service, using the user's own session, so their access applies. |
| `route` | Deterministic routing. The first rule whose conditions hold (`matches`, `equals`, `in`, `exists` on the input or earlier outputs) chooses the next step. Each rule records its reason. |
| `delegate` | Bounded autonomy: a coordinator, an allowlist of action → agent, and `max_delegations` (1–5). |
| `checkpoint` | Human in the loop. The run pauses until someone in `approver_roles` approves or rejects. It can enforce four eyes (the starter may not decide) and can expire. |
| `respond` | A templated final output. |

Rules every workflow must pass:

- **The step graph is acyclic**, so every run ends. All steps must be
  reachable from the start.
- **Templates are plain substitution** (`{{input.x}}`, `{{steps.s.field}}`,
  `{{last.output}}`, `{{review.by}}`, `{{task}}`). Nothing in them is
  evaluated, and an unknown reference is a validation error.
- **No knowledge step after a checkpoint.** A grounded answer needs the
  user's live session token, which is never stored.
- **Every run has a budget:** `max_steps`, `max_agent_calls` and `max_wall_s`.
  Time spent waiting for a person does not count against it.
- **Platform checks at load time.** Every named agent must be in the
  registry. Every role must exist and hold the run permission, and every
  approver role must exist.

## Runs and traces

Runs are stored in the registry database (`workflow_runs`). Each run has an
append-only trace (`workflow_trace`) that records:

- run start, with the workflow version, fingerprint, budget and location;
- each step's start and completion;
- each failed attempt;
- each error path taken;
- each routing decision: the rule taken, its reason and the values it tested;
- each agent call: role (agent, coordinator or delegate), agent, version,
  manifest fingerprint, model, attempts, latency and trace id;
- each capability discovery and each agent fallback;
- each coordinator choice: the raw reply, whether it was accepted, and why
  not;
- each checkpoint: waiting, refused (role or four eyes), decided (by whom,
  with what comment), or expired;
- the final status.

The run's trace id is passed to every agent call and on to the Model Gateway,
so one id follows the whole run. Every agent run made by the engine also
carries `caller` = {workflow, version, run, step, role} in its audit event
and in `x_nexa`.

Run statuses:

- `running`
- `waiting_approval`
- `succeeded`
- `rejected`
- `failed`
- `budget_exceeded`
- `expired`
- `interrupted`: the runtime restarted while the run was executing. It is
  never resumed, because an agent call may have been half done.

## API

The Agent Runtime serves the API. The API Gateway forwards it.

| Method and path | Who |
|---|---|
| `GET /v1/workflows`, `GET /v1/workflows/{id}` | agents.read |
| `POST /v1/workflows/{id}/runs?wait=false` (202, then poll) | the workflow's `allowed_roles` holding its `run_permission` |
| `GET /v1/workflow-runs?status=waiting_approval` (a reviewer's queue) | the starter, the checkpoint's approvers, admin, platform_admin, auditor, ai_engineer |
| `GET /v1/workflow-runs/{run_id}` (with trace) | the same |
| `POST /v1/workflow-runs/{run_id}/decision` `{"decision": "approve" \| "reject", "comment": ""}` | the checkpoint's `approver_roles`, minus the starter when four eyes is set |

## The two demonstration workflows

### `nexa-wf-student-question` (deterministic)

The steps run in this order:

1. **`curriculum`** (knowledge): a grounded answer, general answers off, two
   attempts.
2. **`route`**:
   - if the evidence is `grounded` or `grounded_weak`, go to
     `curriculum_answer`;
   - else, if the question asks for a calculation, go to `calculate`;
   - otherwise go to `explain`.

   If the Knowledge Service is down, the error path goes straight to `route`.
3. One of three endings:
   - **`curriculum_answer`** (respond): the cited curriculum answer, with no
     agent call;
   - **`calculate`** (agent): `nexa-precise`, falling back to `nexa-tutor`;
     it gives a labelled general worked answer;
   - **`explain`** (agent): found by **capability** `explanation` through the
     registry, falling back to `nexa-precise`; it gives a labelled general
     explanation.

The same question with the same evidence always takes the same path.

### `nexa-wf-lesson-explainer` (bounded multi-agent, teachers)

1. **`prepare`** (delegate): the coordinator is `nexa-precise`. The
   allowlist is `explain` → nexa-tutor and `worked_example` →
   nexa-precise, with at most two delegations.
2. **`review`** (checkpoint): a teacher or school administrator, other than
   the teacher who started the run (four eyes), approves or rejects. The
   checkpoint expires after 7 days.
3. **`release`** (respond): the approved material, signed with the
   reviewer's name.

## Acceptance

| Criterion | Evidence |
|---|---|
| A deterministic workflow | `test_deterministic_grounded_answer_needs_no_agent`, `test_deterministic_calculation_path_and_full_trace`, `test_deterministic_same_input_same_path` |
| A bounded multi-agent workflow | `test_bounded_multi_agent_with_human_checkpoint`, `test_coordinator_cannot_leave_the_allowlist`, `test_budget_bounds_the_multi_agent_run` |
| Full traces | Every test above asserts on the trace. `GET /v1/workflow-runs/{id}` returns it. |
| No direct, uncontrolled agent-to-agent calls | `test_an_agent_cannot_call_another_agent`, `test_delegate_agents_never_see_each_other` |
| Routing, delegation, retries, timeouts, fallbacks | Routing: `route.decision` tests. Retries: `test_knowledge_outage_retries_then_takes_the_error_path`. Timeouts: `test_step_timeout_retries_then_fails_the_run`. Fallbacks: `test_fallback_agent_when_the_first_is_disabled`. |
| Human-in-the-loop checkpoints | Four eyes, roles, single decision, reject and expiry tests |
| Bounded autonomy | Allowlist, `max_delegations`, budget, invalid choices refused |
| Contract | `test_workflow_contract_rejects_unsafe_graphs`, `test_workflow_platform_checks`, `test_published_workflow_schema_matches` |

Live check on a running NEXA: `python -m v1.tools.orchestration_check`. It
runs both workflows as real users through the API Gateway and prints every
trace.

## Not in this change

- **Workflows are not in the registry yet.** They are loaded from reviewed
  files at start. Registering and versioning them through the registry API,
  as agents are, can follow the same pattern.
- **The knowledge step is the only tool in a workflow.** Calling tools in
  general waits for the Tool Gateway (port 8105).
- **No parallel steps.** Steps run one after another, which keeps traces and
  budgets simple.
- **Approver notification.** Approvers see waiting runs in their queue
  (`GET /v1/workflow-runs?status=waiting_approval`); there is no email or
  push notification yet.
