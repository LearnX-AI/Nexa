# NEXA-M3-002 — Agent Registry & Discovery Service

Status: implemented (this change). Owner: NEXA AI engineering. Builds on
NEXA-M3-001 (the `nexa.agent/1.0` contract and lifecycle).

## What it is

The authoritative record of every NEXA agent. It lives in the Agent Runtime
(M1-001 assigns the registry to the runtime) and is stored in SQLite
(`NEXA_AGENT_REGISTRY_DB`, default `~/.nexa/agent-registry.sqlite`).

For each agent it records:

| Requirement | Where it is held |
|---|---|
| Identity | `agents` row, plus `identity` in each version's manifest |
| Version | `agent_versions`: one row per (agent, semantic version). Immutable. |
| Status | Lifecycle `state` per version (M3-001). `enabled` per agent is the kill switch. |
| Capabilities | `capabilities` index (agent, version, capability) |
| Required permissions | `permissions.run_permission` and `allowed_roles` in the manifest |
| Deployment location | `deployments`: location (`core:dgx`, `school:pomnsoe`), endpoint, instance |
| Model and tool dependencies | `model.model`, `model.fallbacks`, `tools`, `health.requires` |
| Health | Heartbeat per deployment, with a check for each declared dependency |
| Audit | `events`: append-only; who, what, when, details |

The manifest files in `v1/agent_runtime/agents/` only **seed** the registry.
At start, a file the registry has not seen is registered. A file identical to
a registered version is left alone. A file that changed but kept its version
number stops the runtime from starting, because versions are immutable. After
the seed, lifecycle and enable/disable changes made through the API stand.

## Rules

- **Versions are immutable.** The same agent and version can be registered
  again only with identical content (the *content fingerprint*). Anything else
  gets a 409: raise the version.
- **A new version must be newer** than the newest registered version
  (semantic order).
- **What counts as content.** The content fingerprint is computed over the
  manifest without its lifecycle record (state, approval, deprecation), so
  approving a version changes its state but not its identity. The full
  fingerprint, lifecycle included, is still what `/v1/agents` and
  `x_nexa.manifest` report.
- **Checks on registration.** Every manifest passes M3-001's structural and
  platform checks before it is recorded: models, tools, roles, permissions,
  safety policy.
- **Lifecycle changes.** Only the allowed M3-001 transitions are accepted.
  - Approval records `approved_by` and `approved_on`.
  - Deprecation needs `sunset_on`, and may name `replaced_by`.
  - Activation re-runs the platform checks, so an agent granted a tool that
    is not built cannot be activated.
- **Four eyes.** The identity that registered a version cannot approve it.
- **Several versions coexist.** The *current* version of an agent is its
  highest runnable version (active, or deprecated before its sunset). The
  request path runs the current version, and older versions can still be
  fetched.
- **Disable is separate from the lifecycle.** A disabled agent is refused
  (423, with the reason) whatever its state, and discovery stops offering it.
  Disabling needs a reason; enabling restores the agent at the state it was in.

## Discovery instead of routing tables

`GET /v1/registry/discover?capability=chat&location=school:pomnsoe` returns
the agents that meet all of these:

- they have the capability;
- they are runnable and enabled;
- this caller may run them (role and permission);
- they have at least one healthy deployment.

Each agent comes with its deployments, the caller's location first.
`GET /v1/registry/resolve/{id}?location=` returns the single deployment a
request should go to. Callers choose an agent by capability, and the registry
tells them where it runs. Nothing keeps a table of agent names and hosts.

A deployment counts as healthy only if both are true:

- its last heartbeat is younger than `NEXA_AGENT_HEARTBEAT_STALE_S` (120 s);
- every dependency in the agent's `health.requires` was up at that heartbeat.

The dependency checks are:

- `model_gateway`: `GET /health`;
- `knowledge_service`: `GET /health`;
- `tool_registry`: every granted tool is registered and available;
- `memory_service`: never up, because it is not built yet.

Each Agent Runtime instance records its own deployments at start and sends a
heartbeat every `NEXA_AGENT_HEARTBEAT_S` (30 s). It sets its location from
`NEXA_AGENT_LOCATION`, its endpoint from `NEXA_AGENT_PUBLIC_URL`, and its
instance name from `NEXA_AGENT_INSTANCE`. When a newer version becomes
current, the instance moves its deployment to that version. Only changes in
health are audited, not every beat.

## API

The Agent Runtime serves the API. The API Gateway forwards `/v1/registry/*`
after authenticating the caller, and the runtime enforces the permissions.

| Method and path | Permission |
|---|---|
| `GET /v1/registry/agents?capability=&audience=&state=&runnable=` | agents.read |
| `POST /v1/registry/agents` (manifest body) | agents.register |
| `GET /v1/registry/agents/{id}` (versions, deployments) | agents.read |
| `GET /v1/registry/agents/{id}/versions/{v}` | agents.read |
| `PUT /v1/registry/agents/{id}/versions/{v}/state` to draft, review, approved or active | agents.approve |
| `PUT /v1/registry/agents/{id}/versions/{v}/state` to suspended, deprecated or retired | agents.admin |
| `POST /v1/registry/agents/{id}/disable` and `/enable` | agents.admin |
| `GET /v1/registry/agents/{id}/history`, `GET /v1/registry/history` | agents.read |
| `GET /v1/registry/discover`, `GET /v1/registry/resolve/{id}` | agents.read |
| `POST /v1/registry/deployments`, `POST /v1/registry/deployments/{id}/heartbeat` | agents.deploy |

Who holds each permission (`v1/identity/rbac.py`):

| Permission | Roles |
|---|---|
| `agents.read` | every role except `lms_sync` |
| `agents.register` | ai_engineer, admin, platform_admin |
| `agents.approve` | admin, platform_admin, national_admin |
| `agents.admin` | ai_engineer, admin, platform_admin |
| `agents.deploy` | service, service_account, admin, platform_admin |

The existing `/v1/agents` and `/v1/agents/{name}/run` endpoints are
unchanged. They now also report `enabled` and refuse a disabled agent.

## Acceptance

| Criterion | Evidence |
|---|---|
| Agents can register | `test_register_a_new_version_and_versions_are_immutable`, `test_seed_registers_every_manifest_once` |
| Queried by capability | `test_query_by_capability_audience_and_state`, `test_discover_returns_only_what_the_caller_may_run_where_it_is_healthy` |
| Versioned | `test_full_lifecycle_promotes_the_new_version`, `test_a_promoted_version_moves_the_deployment`, `test_a_changed_manifest_file_without_a_new_version_is_refused` |
| Disabled | `test_disable_is_a_kill_switch_and_is_audited` |
| Audited | `test_registry_audit_trail_covers_every_change`, `test_heartbeat_audits_health_changes_only` |
| No hard-coded routing | Discovery and resolve; `test_api_gateway_forwards_discovery_and_registry_calls` |
| Survives restart | `test_registry_state_survives_a_restart_and_the_file_does_not_override_it` |

Live check on a running NEXA:
`python -m v1.tools.registry_check --url http://127.0.0.1:8099` (18 checks).
It registers nothing new. It disables and then re-enables only
`nexa-lesson-planner`, a draft agent that cannot run anyway.

## Not in this change

- **Federation.** Each runtime, on the DGX and on a school server, keeps its
  own registry. A shared registry that school servers report into needs the
  DGX overflow link from the M2-009 backlog.
- **Client routing by discovery.** `nexa_ask` and the API Gateway still send
  to one runtime URL. Moving them to discovery comes with the second runtime
  location.
- **Approval records.** nexa-tutor and nexa-precise were seeded active from
  the M1 baseline. When Mr. Chandana approves them (M3-001), each gets a
  patch version (1.0.1) that is registered, reviewed, approved in his name
  and activated through this API, so the approval is in the audit trail.
  Editing `approved_by` in the file alone would not be picked up, because
  lifecycle details in a seed file never override the registry.
