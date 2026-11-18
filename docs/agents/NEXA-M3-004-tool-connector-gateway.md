# NEXA-M3-004 — Tool & Connector Gateway

Status: implemented (this change). Owner: NEXA AI engineering.

Governing decisions:

- **ADR-0006 (Tool Gateway).** M1-001 reserved port 8105 for this
  service.
- **ADR-0003 (Agent Runtime).**

It builds on M3-001 (manifest tool grants), M3-002 (registry) and M3-003
(orchestration).

## The boundary

```
agent (text only)  →  Agent Runtime  →  Tool & Connector Gateway  →  connector  →  system
                        checks:            checks:                        (Knowledge Service,
                        agent runnable     caller is the runtime           an approved MCP
                        and enabled        tool active                     server, built-in)
                        manifest grant     allowlist and scopes
                        user permission    user permission
                        max calls per run  argument schema
                                           personal-data egress
                                           rate limits and timeout
                                           output size
                                           → one audit record per call
```

Agents never hold credentials, never see a connector, and never call the
gateway themselves.

- **Who calls the gateway.** Only the Agent Runtime does. It sends three
  things with each call:
  - its own service token (`tools.invoke`, held only by `service` and
    `service_account`);
  - the user's own token;
  - the agent's identity and version.
- **Where calls come from:**
  - `POST /v1/agents/{agent}/tools/{tool}`, as the calling user, through the
    API Gateway;
  - a workflow `tool` step (M3-003), where the engine calls on behalf of the
    step's agent.

## Contracts: `nexa.tool/1.0`, independent of how a tool is built

Each tool is a reviewed file in `v1/tool_gateway/tools/`. Its schema is
published in `v1/tool_gateway/schema/nexa-tool-1.0.schema.json` and checked
in CI.

A contract says:

- what the tool does: identity, description, input schema;
- which rules apply:
  - the permission the user must hold;
  - named scopes, each with the roles that may use it;
  - the allowlist: which agents, with which scopes;
  - timeout, rate limits and output cap;
  - side effects;
  - what data it returns, and whether arguments leave NEXA.

It does not say how the tool is built. `implementation` binds it to code
inside NEXA (`builtin`), an HTTP API (`http`) or an MCP server (`mcp`),
through a connector, so the same contract can move from one implementation
to another without any agent changing.

Rules every contract must pass:

- the input schema is an object with `additionalProperties: false`;
- a tool with side effects, or one returning teacher-only data, has no
  student scope;
- every allowlist grant names the tool's own scopes;
- every binding resolves to a real handler or connector;
- an **active tool may not use an MCP connector that is not approved**
  (with `approved_by` recorded).

## Connectors and credential isolation

A connector (`v1/tool_gateway/connectors/`) says where a system is, as a URL
from configuration, and how the gateway authenticates to it:

| Mode | Meaning |
|---|---|
| `none` | No credential. |
| `user_delegated` | The user's own NEXA session, so the system applies that user's access. The Knowledge Service sees the student, not a service. |
| `service` | A secret read from the gateway's own environment (`/etc/nexa/tool-gateway.env`, root-readable). |

A service secret never appears in:

- a contract or the repository;
- a response to an agent (a response containing it is refused);
- the audit trail.

Handlers get a connection that can make requests, never the credential
itself.

**MCP** works through the streamable HTTP transport: `initialize`,
`notifications/initialized`, then `tools/call`, with session ids and JSON or
SSE responses. The NEXA contract names the MCP tool, and nothing about MCP
reaches the agent.

## Tools in this change

| Tool | Implementation | Scopes | Allowlist |
|---|---|---|---|
| `knowledge_answer` | http → Knowledge Service `/v1/knowledge/answer`, user-delegated | `curriculum.learner` (all roles), `curriculum.teacher` (staff) | nexa-curriculum-tutor (learner), nexa-lesson-planner (learner, teacher) |
| `course_outline` | http → Knowledge Service (outline answers), user-delegated | same | nexa-lesson-planner |
| `calculator` | builtin: exact arithmetic on fractions (+ − × ÷ ^ % ( ), sqrt, abs, round). Nothing else is evaluated. Replaces the v0 SymPy maths engine for worked answers. | `compute` | nexa-curriculum-tutor, nexa-lesson-planner |

Agent changes:

- **`knowledge_answer` and `course_outline`** were declared in M3-001 but
  not built. They now exist, so the agents waiting for them can be approved
  and activated.
- **`nexa-curriculum-tutor` 0.2.0** (in review) also grants `calculator`
  and adds the `calculation` capability.
- **New workflow `nexa-wf-curriculum-tutor`** uses both tools:
  - a calculation question: the agent writes the expression, the
    calculator computes it exactly, then the agent explains the working;
  - any other question: `knowledge_answer` as the student, then a route on
    the evidence.

## Audit

Every call writes exactly one record, allowed or refused, to SQLite
(`NEXA_TOOL_AUDIT_DB`) and to the structured audit log. A record holds:

- time, call id, trace id, workflow run and step;
- the user and roles, the agent and version;
- the tool, version, implementation, connector and the scopes used;
- the argument digest, and a preview with personal data masked;
- the decision, **the check that decided it** and why;
- the status, latency, output size and output digest.

A record never holds a credential, and never the full arguments or output.
Auditors read records with `GET /v1/tools/audit?trace_id=&run_id=&tool=&user=&call_id=`
(auditor, admin, platform_admin, ai_engineer).

## Acceptance

| Criterion | Evidence |
|---|---|
| At least two tools invoked through the gateway with authorization | `knowledge_answer` and `calculator`: `test_knowledge_answer_through_the_gateway_as_the_user`, `test_calculator_through_the_gateway`, `test_workflow_tool_steps_with_full_trace_and_audit` |
| Complete audit traces | Every test above checks the audit record: fields, call id, trace id, run and step |
| Allowlists and scopes | `test_allowlist_permission_scopes_and_arguments` |
| Credential isolation | `test_mcp_tool_with_an_isolated_service_credential`: the secret is used, never returned or audited, and a response echoing it is refused |
| Timeouts | `test_tool_timeout_and_unreachable_connector` |
| Rate limits | `test_rate_limit_per_user` (per user and per tool), `test_max_calls_per_run` |
| Policy checks | arguments, data egress, side effects, output size; `test_contract_rules`, `test_calculator_is_exact_and_refuses_anything_else` |
| MCP-compatible where approved | `test_mcp_tool_with_an_isolated_service_credential`, `test_an_active_tool_cannot_use_an_unapproved_mcp_server` |
| Runtime checks first | `test_runtime_checks_before_the_gateway` (not granted, draft agent, disabled: nothing reaches the gateway) |

Live check: `python -m v1.tools.tool_gateway_check`. It runs both tools as a
student through the API Gateway, plus refusals and a workflow, and prints
the audit record of every call.

## Deploying

The gateway is a new service, `nexa-tool-gateway` on port 8105. Its unit
file is in `v1/deploy/`, and `deploy.sh` now restarts and health-checks it.

`nexa-curriculum-tutor` 0.2.0 must be **approved and activated** in the
registry before it can use tools, for example with:

```
python -m v1.tools.agent_lifecycle nexa-curriculum-tutor 0.2.0 --to active --approved-by "<approver>" --actor "user:<you>"
```

Approving a student-facing agent is a governance decision.

## Not in this change

- No MCP server is approved in the repository. The connector type and its
  governance are in place, and the tests use a test server. An MCP server
  is added by reviewing a connector file with `approved_by`.
- No write tools yet. The contract supports `side_effects: write`, but
  nothing ships with it.
- Rate limits are counted per gateway process, which is one per host.
