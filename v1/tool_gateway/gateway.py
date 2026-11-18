"""The Tool Gateway's call path (NEXA-M3-004, ADR-0006).

invoke() takes one call from the Agent Runtime and applies, in order, the
checks in policy.py, then runs the tool under its timeout, checks the output,
and records the call in the audit trail. Whatever happens, exactly one audit
record is written per call.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from v1.identity.principal import Principal
from v1.identity.rbac import can

from . import contract as ct
from .audit import Audit, digest, preview
from .connectors import Connection, ConnectorError
from .handlers import HANDLERS, Call, HandlerError
from .policy import PolicyError, find_pii, rate_check, resolve_scopes, validate_args

TOOLS: dict[str, ct.ToolContract] = {}
CONNECTORS: dict[str, ct.ConnectorContract] = {}
AUDIT: Optional[Audit] = None


def load(tools_dir: Optional[str] = None, connectors_dir: Optional[str] = None,
         audit_path: Optional[str] = None) -> None:
    global AUDIT
    tools, connectors = ct.load(tools_dir or os.environ.get("NEXA_TOOL_DIR") or ct.TOOLS_DIR,
                                connectors_dir or os.environ.get("NEXA_CONNECTOR_DIR") or ct.CONNECTORS_DIR,
                                handlers=set(HANDLERS))
    TOOLS.clear()
    TOOLS.update(tools)
    CONNECTORS.clear()
    CONNECTORS.update(connectors)
    if AUDIT is not None:
        AUDIT.close()
    AUDIT = Audit(audit_path)


@dataclass
class Caller:
    service: Principal            # the Agent Runtime
    user: Principal               # on whose behalf
    user_token: str
    agent: str
    agent_version: Optional[str]
    run_id: Optional[str]
    step: Optional[str]
    trace_id: Optional[str]
    delegation: Optional[dict] = None   # the verified claims of the delegated token (NEXA-M3-007)


class GatewayError(Exception):
    def __init__(self, status_code: int, detail: str, call_id: str):
        self.status_code, self.detail, self.call_id = status_code, detail, call_id
        super().__init__(detail)


_STATUS = {"unavailable": 503, "timeout": 504, "refused": 403, "error": 502}


async def invoke(tool_id: str, caller: Caller, arguments: Any, scopes: Optional[list[str]] = None) -> dict[str, Any]:
    call_id = "tc_" + uuid.uuid4().hex[:20]
    t0 = time.monotonic()
    tool = TOOLS.get(tool_id)
    rec: dict[str, Any] = dict(call_id=call_id, trace_id=caller.trace_id, run_id=caller.run_id, step=caller.step,
                               user=caller.user.subject, roles=list(caller.user.roles), agent=caller.agent,
                               agent_version=caller.agent_version, tool=tool_id,
                               tool_version=tool.version if tool else None,
                               kind=tool.implementation.kind if tool else None,
                               connector=tool.implementation.connector if tool else None,
                               args_digest=digest(arguments), args_preview=preview(arguments))

    def refuse(status: int, check: str, reason: str):
        rec.update(decision="deny", check_name=check, reason=reason, status=status,
                   latency_ms=int((time.monotonic() - t0) * 1000))
        AUDIT.record(**rec)
        return GatewayError(status, reason, call_id)

    # 1. caller
    if not can(caller.service, "tools.invoke"):
        raise refuse(403, "caller", "only the Agent Runtime may call tools")
    # 1b. delegation (NEXA-M3-007): the user's authority reaches the gateway
    # only as a delegated token naming this agent, for this gateway, with the
    # scopes the agent's policy granted; never the user's own token.
    d = caller.delegation or {}
    act = f"agent:{caller.agent}@{caller.agent_version or '?'}"
    if d.get("typ") != "delegated":
        raise refuse(403, "delegation", "a delegated token is required, not the user's own")
    if d.get("act") != act:
        raise refuse(403, "delegation", f"the token was delegated to {d.get('act')}, not {act}")
    if d.get("nexa_aud") != "nexa-tool-gateway":
        raise refuse(403, "delegation", "the token is not for the Tool Gateway")
    if d.get("run_id") and caller.run_id and d["run_id"] != caller.run_id:
        raise refuse(403, "delegation", "the token belongs to another run")
    token_scopes = list(d.get("scope") or [])
    scopes = scopes or token_scopes
    if set(scopes) - set(token_scopes):
        raise refuse(403, "delegation", f"scopes {sorted(set(scopes) - set(token_scopes))} were not delegated")
    # 2. tool
    if tool is None:
        raise refuse(404, "tool", f"unknown tool {tool_id}")
    if tool.state != "active":
        raise refuse(409 if tool.state == "draft" else 410, "tool", f"tool {tool_id} is {tool.state}")
    # 3. allowlist
    granted = tool.allowed_agents.get(caller.agent)
    if granted is None:
        raise refuse(403, "allowlist", f"agent {caller.agent} is not on the allowlist of {tool_id}")
    # 4. permission
    if not can(caller.user, tool.permission):
        raise refuse(403, "permission", f"the user does not hold {tool.permission}")
    # 5. scopes
    try:
        used = resolve_scopes(tool, scopes, granted, caller.user.roles)
    except PolicyError as exc:
        raise refuse(exc.status_code, exc.check, exc.detail)
    rec["scopes"] = used
    # 6. arguments
    problems = validate_args(tool.input_schema, arguments)
    if problems:
        raise refuse(422, "arguments", "; ".join(problems[:5]))
    # 7. data
    if tool.data.leaves_nexa:
        pii = find_pii(arguments)
        if pii:
            raise refuse(422, "data", f"personal data ({', '.join(pii)}) may not leave NEXA")
    # 8. rate
    try:
        rate_check(tool, caller.user.subject)
    except PolicyError as exc:
        raise refuse(exc.status_code, exc.check, exc.detail)

    # Run it.
    try:
        result = await asyncio.wait_for(_run(tool, caller, arguments, used), timeout=tool.timeout_s)
    except asyncio.TimeoutError:
        result, err = None, (504, "timeout", f"{tool_id} timed out after {tool.timeout_s:.0f}s")
    except ConnectorError as exc:
        result, err = None, (_STATUS.get(exc.kind, 502), "connector", str(exc))
    except HandlerError as exc:
        result, err = None, (exc.status_code, "tool", exc.detail)
    else:
        err = None
    latency = int((time.monotonic() - t0) * 1000)
    if err:
        rec.update(decision="allow", check_name=err[1], reason=err[2], status=err[0], latency_ms=latency)
        AUDIT.record(**rec)
        raise GatewayError(err[0], err[2], call_id)
    body = json.dumps(result, ensure_ascii=False, default=str)
    if len(body.encode()) > tool.max_output_bytes:
        rec.update(decision="allow", check_name="output", reason=f"output over {tool.max_output_bytes} bytes",
                   status=502, latency_ms=latency, output_bytes=len(body.encode()))
        AUDIT.record(**rec)
        raise GatewayError(502, f"{tool_id} returned more than {tool.max_output_bytes} bytes", call_id)
    rec.update(decision="allow", check_name=None, reason=None, status=200, latency_ms=latency,
               output_bytes=len(body.encode()), output_digest=digest(result))
    AUDIT.record(**rec)
    return {"call_id": call_id, "tool": tool_id, "version": tool.version, "scopes": used, "result": result,
            "x_nexa": {"trace_id": caller.trace_id, "latency_ms": latency, "implementation": tool.implementation.kind,
                       "connector": tool.implementation.connector}}


async def _run(tool: ct.ToolContract, caller: Caller, arguments: dict[str, Any], scopes: list[str]) -> dict:
    im = tool.implementation
    conn = None
    if im.kind in ("http", "mcp"):
        conn = Connection(CONNECTORS[im.connector], user_token=caller.user_token, trace_id=caller.trace_id,
                          timeout=tool.timeout_s)
    if im.kind == "mcp":
        out = await conn.mcp_call(im.mcp_tool, arguments)
        return {**out, "text": out["text"]}
    return await HANDLERS[im.handler](arguments, Call(caller.user, scopes, conn, caller.trace_id))


def catalog(principal: Principal) -> list[dict[str, Any]]:
    """The tools, as an agent author sees them: contract, not implementation secrets."""
    out = []
    for t in TOOLS.values():
        out.append({"id": t.id, "name": t.name, "version": t.version, "state": t.state, "description": t.description,
                    "permission": t.permission, "scopes": {k: v.model_dump() for k, v in t.scopes.items()},
                    "default_scopes": t.default_scopes, "allowed_agents": t.allowed_agents,
                    "input_schema": t.input_schema, "timeout_s": t.timeout_s, "rate_limit": t.rate_limit.model_dump(),
                    "side_effects": t.side_effects, "data": t.data.model_dump(),
                    "implementation": t.implementation.kind, "connector": t.implementation.connector,
                    "usable_by_you": can(principal, t.permission)})
    return out


def connectors_view() -> list[dict[str, Any]]:
    return [{"id": c.id, "name": c.name, "kind": c.kind, "owner": c.owner, "approved": c.approved,
             "approved_by": c.approved_by, "credential": c.credential.mode,
             "credential_configured": c.credential.mode != "service" or bool(os.environ.get(c.credential.env or "")),
             "url_configured": bool(c.url())}
            for c in CONNECTORS.values()]
