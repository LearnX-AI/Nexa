"""NEXA tool and connector contracts (nexa.tool/1.0, NEXA-M3-004, ADR-0006).

A tool is what an agent may use: a reviewed file in v1/tool_gateway/tools/.
Its contract says what the tool does and under which rules, never how it is
built: the same contract can be served by code inside NEXA (builtin), an HTTP
API (http) or an approved MCP server (mcp), through a connector.

  id, version, description    identity
  state                       draft | active | retired; only active tools run
  input_schema                JSON Schema (object) for the arguments
  permission                  the NEXA permission the user must hold
  scopes                      named data scopes, each with the roles that may
                              use it; a call uses its default scopes unless it
                              asks for others the agent is granted
  allowed_agents              the allowlist: the agents that may use the tool,
                              each with the scopes granted to it
  timeout_s, rate_limit       per call; per user and per tool, per minute
  side_effects                read | write
  data                        what the tool returns (public, learner,
                              teacher_only) and whether arguments leave NEXA
  implementation              kind and binding (builtin handler, connector and
                              path, or connector and MCP tool name)

A connector is how NEXA reaches a system: v1/tool_gateway/connectors/. It
holds where the system is (a URL, from configuration) and how the gateway
authenticates to it: none, the user's own session (user_delegated), or a
service credential read from the gateway's environment (service). Credentials
stay inside the gateway: never in a contract, a response or the audit trail.
An MCP connector must be approved before any tool may bind to it.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TOOL_VERSION = "nexa.tool/1.0"
ROOT = os.path.dirname(__file__)
TOOLS_DIR = os.path.join(ROOT, "tools")
CONNECTORS_DIR = os.path.join(ROOT, "connectors")
TOOL_ID = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
CONNECTOR_ID = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
ENV_NAME = re.compile(r"^NEXA_[A-Z0-9_]+$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scope(Strict):
    description: str = Field(min_length=5)
    roles: list[str] = Field(min_length=1)


class RateLimit(Strict):
    per_user_per_minute: int = Field(30, ge=1, le=6000)
    per_tool_per_minute: int = Field(600, ge=1, le=60000)


class DataPolicy(Strict):
    returns: Literal["public", "learner", "teacher_only"] = "public"
    leaves_nexa: bool = Field(False, description="Arguments are sent outside NEXA: personal data is refused.")


class Implementation(Strict):
    kind: Literal["builtin", "http", "mcp"]
    handler: Optional[str] = Field(None, description="builtin: the handler name.")
    connector: Optional[str] = Field(None, description="http and mcp: the connector id.")
    path: Optional[str] = Field(None, description="http: the request path on the connector.")
    mcp_tool: Optional[str] = Field(None, description="mcp: the tool's name on the MCP server.")

    @model_validator(mode="after")
    def _binding(self) -> "Implementation":
        need = {"builtin": ("handler",), "http": ("connector", "handler"), "mcp": ("connector", "mcp_tool")}
        for f in need[self.kind]:
            if not getattr(self, f):
                raise ValueError(f"a {self.kind} tool names its {f}")
        return self


class ToolContract(Strict):
    tool_version: Literal["nexa.tool/1.0"]
    id: str
    name: str = Field(min_length=3, max_length=80)
    version: str
    owner: str = Field(min_length=2)
    description: str = Field(min_length=10, max_length=600)
    state: Literal["draft", "active", "retired"]
    input_schema: dict[str, Any]
    permission: str
    scopes: dict[str, Scope] = Field(min_length=1)
    default_scopes: list[str] = Field(min_length=1)
    allowed_agents: dict[str, list[str]] = Field(
        min_length=1, description="The allowlist: agent id -> the scopes granted to it.")
    timeout_s: float = Field(30.0, ge=0.5, le=300.0)
    rate_limit: RateLimit = Field(default_factory=RateLimit)
    max_output_bytes: int = Field(65536, ge=256, le=1048576)
    side_effects: Literal["read", "write"] = "read"
    data: DataPolicy = Field(default_factory=DataPolicy)
    implementation: Implementation

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not TOOL_ID.match(v):
            raise ValueError("tool id is lower snake case")
        return v

    @field_validator("version")
    @classmethod
    def _ver(cls, v: str) -> str:
        if not SEMVER.match(v):
            raise ValueError("version must be MAJOR.MINOR.PATCH")
        return v

    @model_validator(mode="after")
    def _rules(self) -> "ToolContract":
        s = self.input_schema
        if s.get("type") != "object" or s.get("additionalProperties", True) is not False:
            raise ValueError("input_schema is an object with additionalProperties: false")
        unknown = set(self.default_scopes) - set(self.scopes)
        if unknown:
            raise ValueError(f"default_scopes not declared: {sorted(unknown)}")
        for agent, granted in self.allowed_agents.items():
            if not granted or set(granted) - set(self.scopes):
                raise ValueError(f"allowed_agents.{agent}: grant one or more of the tool's scopes")
        if self.side_effects == "write" and "student" in {r for sc in self.scopes.values() for r in sc.roles}:
            raise ValueError("a tool with side effects is never available to students")
        if self.data.returns == "teacher_only" and "student" in {r for sc in self.scopes.values() for r in sc.roles}:
            raise ValueError("a tool returning teacher-only data has no scope for students")
        return self


class Credential(Strict):
    mode: Literal["none", "user_delegated", "service"]
    env: Optional[str] = Field(None, description="service: the environment variable holding the secret.")
    header: str = "Authorization"
    scheme: str = "Bearer"

    @model_validator(mode="after")
    def _env(self) -> "Credential":
        if self.mode == "service" and not (self.env and ENV_NAME.match(self.env)):
            raise ValueError("a service credential names its NEXA_* environment variable")
        if self.mode != "service" and self.env:
            raise ValueError("only a service credential reads an environment variable")
        return self


class ConnectorContract(Strict):
    tool_version: Literal["nexa.tool/1.0"]
    id: str
    name: str = Field(min_length=3)
    kind: Literal["http", "mcp"]
    owner: str = Field(min_length=2)
    url_env: str = Field(description="Environment variable with the base URL.")
    default_url: Optional[str] = None
    credential: Credential
    approved: bool = Field(False, description="Reviewed and approved for use. MCP connectors must be.")
    approved_by: Optional[str] = None
    timeout_s: float = Field(30.0, ge=0.5, le=300.0)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not CONNECTOR_ID.match(v):
            raise ValueError("connector id is lower kebab case")
        return v

    @model_validator(mode="after")
    def _approval(self) -> "ConnectorContract":
        if not ENV_NAME.match(self.url_env):
            raise ValueError("url_env is a NEXA_* environment variable")
        if self.approved and not self.approved_by:
            raise ValueError("an approved connector records approved_by")
        return self

    def url(self) -> Optional[str]:
        u = os.environ.get(self.url_env) or self.default_url
        return u.rstrip("/") if u else None


def _parse(model, path: str):
    with open(path) as fh:
        raw = json.load(fh)
    try:
        return model.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
        errs = getattr(exc, "errors", None)
        raise ContractError(path, [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in errs()]
                            if errs else [str(exc)]) from None


class ContractError(Exception):
    def __init__(self, path: str, problems: list[str]):
        self.path, self.problems = path, problems
        super().__init__(f"{path}: " + "; ".join(problems))


def load(tools_dir: str = TOOLS_DIR, connectors_dir: str = CONNECTORS_DIR,
         handlers: Optional[set[str]] = None) -> tuple[dict[str, ToolContract], dict[str, ConnectorContract]]:
    """Every contract, cross-checked: bindings resolve, MCP connectors are approved."""
    connectors: dict[str, ConnectorContract] = {}
    for f in sorted(os.listdir(connectors_dir)) if os.path.isdir(connectors_dir) else []:
        if f.endswith(".json"):
            c = _parse(ConnectorContract, os.path.join(connectors_dir, f))
            if f != f"{c.id}.json":
                raise ContractError(f, [f"file must be named {c.id}.json"])
            connectors[c.id] = c
    tools: dict[str, ToolContract] = {}
    for f in sorted(os.listdir(tools_dir)) if os.path.isdir(tools_dir) else []:
        if not f.endswith(".json"):
            continue
        path = os.path.join(tools_dir, f)
        t = _parse(ToolContract, path)
        if f != f"{t.id}.json":
            raise ContractError(path, [f"file must be named {t.id}.json"])
        problems = check(t, connectors, handlers)
        if problems:
            raise ContractError(path, problems)
        tools[t.id] = t
    return tools, connectors


def check(t: ToolContract, connectors: dict[str, ConnectorContract], handlers: Optional[set[str]]) -> list[str]:
    from v1.identity.rbac import ROLE_PERMISSIONS
    problems = []
    all_perms = set().union(*ROLE_PERMISSIONS.values())
    if t.permission not in all_perms:
        problems.append(f"permission {t.permission!r} is not a NEXA permission")
    for name, sc in t.scopes.items():
        for r in sc.roles:
            if r not in ROLE_PERMISSIONS:
                problems.append(f"scope {name!r}: role {r!r} is not a NEXA role")
    im = t.implementation
    if im.kind == "builtin" and handlers is not None and im.handler not in handlers:
        problems.append(f"builtin handler {im.handler!r} does not exist")
    if im.kind in ("http", "mcp"):
        c = connectors.get(im.connector)
        if c is None:
            problems.append(f"connector {im.connector!r} is not defined")
        elif c.kind != im.kind:
            problems.append(f"connector {im.connector!r} is {c.kind}, the tool is {im.kind}")
        elif im.kind == "mcp" and not c.approved and t.state == "active":
            problems.append(f"MCP connector {im.connector!r} is not approved, so an active tool may not use it")
        elif im.kind == "http" and handlers is not None and im.handler not in handlers:
            problems.append(f"http handler {im.handler!r} does not exist")
    return problems


def json_schema() -> dict[str, Any]:
    s = ToolContract.model_json_schema()
    s["title"] = "NEXA tool contract (nexa.tool/1.0)"
    return s
