# NEXA-M3-007 — Agent Authorization, Permissions & Trust Enforcement

Status: implemented (this change). Owner: NEXA AI engineering.

Builds on:

- NEXA identity: tokens, roles and RBAC in `v1/identity`;
- M3-001 (manifests);
- M3-003 (orchestration);
- M3-004 (Tool Gateway);
- M3-006 (state).

Code:

- policies: `v1/agent_runtime/policy/contract.py`, with one file per agent
  in `policy/agents/`;
- the decision point: `policy/pdp.py`;
- the API: `policy/api.py`;
- the published schema: `schema/nexa-agent-policy-1.0.schema.json`;
- security tests: `v1/tests/test_agent_security.py`, run as their own CI step.

## Principle: least privilege, default deny, one decision point

Every agent has a reviewed **agent policy** (`nexa.agent-policy/1.0`) that
says what it may **read**, **write**, **invoke** and **delegate**. An agent
without a policy can do nothing, and anything a policy does not list is
refused.

A policy can only narrow what the agent's manifest declares, never widen it.
At load time and in CI, every policy is checked against:

- the agent's manifest;
- the tool contracts;
- the other policies.

| Policy field | Meaning | Checked against |
|---|---|---|
| `read.knowledge_classes` | Knowledge access classes the agent may receive (`learner`, `teacher_only`). | The manifest's `permissions.knowledge.access_classes`. |
| `write` | Tools with side effects it may use. | Tool contracts (`side_effects: write`). None are granted today. |
| `invoke` | Tool → the scopes it may use. | The manifest's tool grants and the Tool Gateway's allowlist. A scope that reaches a knowledge class must be in `read`. |
| `delegate` | Agents it may hand work to as a coordinator. | Registered agents that accept delegation. |
| `accept_delegation` | Whether a coordinator may hand it work. | — |
| `tenancy` | `user_school`: it acts only in the calling user's school context. | — |
| `sensitive.redact_personal_data` | Email addresses and phone numbers are removed from its output. | Required when the manifest says `serves_minors`. |

What each agent is granted today:

| Agent | Read | Invoke | Delegate | Accepts delegation | Redacts |
|---|---|---|---|---|---|
| nexa-tutor | — | — | — | yes | yes |
| nexa-precise | — | — | nexa-tutor, nexa-precise | yes | yes |
| nexa-curriculum-tutor | learner | knowledge_answer (`curriculum.learner`), calculator (`compute`) | — | no | yes |
| nexa-lesson-planner | learner, teacher_only | knowledge_answer and course_outline (both scopes) | — | no | no (teachers only) |

## Enforcement

**The decision point** (`pdp.py`) decides. The services enforce, and every
decision, allowed or denied, is one row in `policy_decisions` and one audit
event. A record holds:

- the agent and version, as its service identity `agent:<id>@<version>`;
- the user, roles and school (the tenant);
- the action and resource;
- the decision, **the rule that decided**, and the reason;
- the trace and run.

| Where | What is decided | Rules |
|---|---|---|
| Agent Runtime, before an agent runs | `run` | default deny, role, permission |
| Agent Runtime, before a tool call | `invoke` | default deny, tool in policy, scopes within the policy, knowledge class within `read`, never teacher-only material for a student, writes only if listed |
| Orchestration engine, before a coordinator's choice is sent | `delegate` | coordinator's `delegate`, target's `accept_delegation` |
| Orchestration engine, decisions and visibility at a checkpoint | `decide`, `view` | tenancy: a reviewer must be from the run's school (national oversight roles excepted) |
| Agent Runtime, on every output | `redact` | personal data removed for agents that must redact |
| Workflows, when they load | — | a workflow may not ask of an agent what its policy does not allow |

These come on top of the earlier layers, which still apply:

- manifest lifecycle, roles and tool grants (M3-001);
- the kill switch (M3-002);
- the Tool Gateway's allowlist, scopes, schema, data and rate checks (M3-004);
- the message routing rules (M3-005);
- state policy (M3-006).

## Service identities and delegated authority

- **Each service calls with its own identity.** The Agent Runtime presents
  its service token to the Model Gateway and the Tool Gateway. Only
  `service` and `service_account` hold `tools.invoke`.
- **An agent acts as `agent:<id>@<version>`.** That identity appears in
  every decision, every Tool Gateway audit record and every delegated token.
- **The user's own token never leaves the Agent Runtime.** For a tool call,
  the runtime mints a **delegated token** valid for 2 minutes. It holds:
  - `typ` `delegated`;
  - the user's subject, roles, school and classes;
  - `act`: the agent's identity;
  - `scope`: only the scopes the policy just granted;
  - `nexa_aud`: `nexa-tool-gateway`;
  - the run.
- **The Tool Gateway** refuses the user's own token, a delegation for
  another agent, for another service or for another run, and any scope that
  was not delegated.
- **The Knowledge Service** accepts a delegated token only if it is meant
  for the Tool Gateway, and **narrows the user's access to the delegated
  scopes**. A teacher using the student tutor gets learner material only.
- **The API Gateway and the Agent Runtime refuse delegated tokens** (401).
  A token an agent's tool call carried can never be replayed as the user.

## Tenant context

The user's school (`school_id`) and classes travel with the principal into
workflow runs. They are kept with the run, so a resumed run still knows
them, and they go into delegated tokens.

- A run started in a school is visible to, and decided by, reviewers of
  that school only. National oversight roles (admin, platform_admin,
  national_admin) are the exception.
- The Knowledge Service applies the school to what it returns (M2-005).

## API

- `GET /v1/policy/agents`, `GET /v1/policy/agents/{id}`: the policies and
  their fingerprints.
- `GET /v1/policy/decisions?agent=&action=&decision=&trace_id=&run_id=&actor=`:
  the decision log, for auditor, admin, platform_admin and ai_engineer.
- `GET /v1/policy/schema`

The API Gateway forwards these routes, read-only.

## Acceptance: unauthorized agent actions are blocked by policy, verified by security tests

| Attempt | Blocked by | Test |
|---|---|---|
| An agent with no policy acts | default deny | `test_an_agent_without_a_policy_can_do_nothing` |
| A role the agent does not serve runs it | role | `test_a_role_the_agent_does_not_serve_is_refused` |
| A tool outside a policy narrower than the manifest | invoke | `test_policy_narrower_than_the_manifest_blocks_the_tool` |
| Scope escalation | scopes | `test_an_agent_cannot_escalate_its_scopes` |
| Teacher-only material for a student | sensitive | `test_teacher_only_material_is_never_retrieved_for_a_student` |
| The user's token leaving the runtime | delegated tokens | `test_the_user_token_never_leaves_the_runtime` |
| A forged or misdirected delegation at the Tool Gateway | delegation | `test_gateway_accepts_only_a_matching_delegation` |
| A delegated token replayed as the user | 401 | `test_a_delegated_token_cannot_be_replayed_as_the_user` |
| A delegated token reaching beyond its scopes | Knowledge narrowing | `test_knowledge_service_narrows_a_delegated_token_to_its_scopes` |
| Delegating outside the coordinator's policy | delegate | `test_a_coordinator_cannot_delegate_outside_its_policy` |
| Delegating to an agent that does not accept it | accept_delegation | `test_an_agent_that_does_not_accept_delegation_is_refused` |
| A reviewer from another school | tenancy | `test_reviewers_of_another_school_cannot_see_or_decide` |
| Personal data in a minor-facing agent's output | redact | `test_personal_data_is_removed_from_agents_that_serve_minors` |
| A policy that exceeds its manifest | load and CI check | `test_a_policy_can_never_exceed_its_manifest` |
| Missing policy or schema drift | CI | `test_every_agent_has_a_policy_and_the_schema_is_published` |
| Decisions not audited | decision log | `test_every_decision_is_audited_and_readable_by_auditors` |

Live check: `python -m v1.tools.security_check` attempts the same actions on
the running DGX through the API Gateway, then reads the decision log as an
auditor. `test_live_security_check_passes_against_the_real_apps` runs it in
process.

## Not in this change

- **Per-agent signing keys.** Agent identity is asserted by the Agent
  Runtime (`act` in tokens it signs), not by a key per agent. Agents run
  inside the runtime, so per-agent keys would add no separation today.
- **Tenancy for school servers.** It is enforced on the DGX for workflow
  review and visibility, and by the Knowledge Service for content. Each
  school server's own registry (see M3-002, federation) keeps its own
  decisions.
