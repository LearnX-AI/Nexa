"""Tool invocation boundary (NEXA-ADR-0003, NEXA-M1-004).

Every tool call an agent makes goes through invoke_tool(). It enforces, in
order: the tool is declared on the agent, the tool exists, the caller holds the
tool's required permission. Each decision is audited and the call is traced
under the request's trace id. The Tool Gateway (NEXA-ADR-0006) arrives in a
later phase; until then tools are small in-process functions registered here,
and no agent is given tools by default.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from v1.identity.principal import Principal
from v1.identity.rbac import can
from v1.observability import audit_event, span

from . import registry


class ToolError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: Callable[[dict], Any]
    required_permission: str = "model.invoke"


TOOLS: dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec) -> None:
    TOOLS[spec.name] = spec


def _current_time(args: dict) -> dict:
    return {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}


register_tool(ToolSpec("current_time", "Return the current UTC time.", _current_time))


def invoke_tool(
    agent_name: str,
    tool_name: str,
    principal: Principal,
    args: Optional[dict] = None,
    trace_id: Optional[str] = None,
) -> Any:
    action = f"tool.invoke:{agent_name}:{tool_name}"
    agent = registry.get_agent(agent_name)
    if agent is None:
        raise ToolError(404, f"Unknown agent: {agent_name}")
    spec = TOOLS.get(tool_name)
    if tool_name not in agent.tools or spec is None:
        audit_event(action=action, decision="deny", principal=principal.subject,
                    reason="tool not declared or unknown", trace_id=trace_id)
        raise ToolError(403, "Tool not permitted for this agent")
    if not can(principal, spec.required_permission):
        audit_event(action=action, decision="deny", principal=principal.subject,
                    permission=spec.required_permission, trace_id=trace_id)
        raise ToolError(403, "Not permitted")
    audit_event(action=action, decision="allow", principal=principal.subject,
                permission=spec.required_permission, trace_id=trace_id)
    with span(f"tool.{tool_name}", trace_id, agent=agent_name):
        return spec.handler(args or {})
