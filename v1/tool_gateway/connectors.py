"""Connectors of the Tool Gateway (NEXA-M3-004): how NEXA reaches a system.

Credentials are isolated here. A tool handler gets a Connection, which can
make requests, never the credential itself:

  none            no credential
  user_delegated  the user's own NEXA session, so the system applies that
                  user's access (the Knowledge Service, for example)
  service         a secret read from the gateway's own environment
                  (/etc/nexa/tool-gateway.env), never from a contract,
                  never returned, never written to the audit trail; any
                  response that contains it is refused

MCP: an approved MCP server reached over HTTP (JSON-RPC 2.0, the streamable
HTTP transport): initialize, then tools/call. The NEXA tool contract names the
MCP tool; nothing about MCP leaks into the contract the agent sees.
"""
from __future__ import annotations

import itertools
import json
import os
from typing import Any, Optional

import httpx

from v1.observability import TRACE_HEADER

from .contract import ConnectorContract

MCP_PROTOCOL = "2025-06-18"
_ids = itertools.count(1)

# Tests replace this with an in-process transport.
TRANSPORT: Optional[httpx.AsyncBaseTransport] = None


class ConnectorError(Exception):
    """kind: unavailable | timeout | refused | error"""
    def __init__(self, message: str, kind: str = "error", status: Optional[int] = None):
        self.kind, self.status = kind, status
        super().__init__(message)


class Connection:
    def __init__(self, contract: ConnectorContract, *, user_token: Optional[str], trace_id: Optional[str],
                 timeout: float):
        self.contract, self.trace_id, self.timeout = contract, trace_id, min(timeout, contract.timeout_s)
        self._secret: Optional[str] = None
        cred = contract.credential
        if cred.mode == "user_delegated":
            if not user_token:
                raise ConnectorError(f"{contract.id} needs the user's session", "refused")
            self._secret = user_token
        elif cred.mode == "service":
            self._secret = os.environ.get(cred.env or "")
            if not self._secret:
                raise ConnectorError(f"{contract.id}: its credential is not configured on this gateway",
                                     "unavailable")
        base = contract.url()
        if not base:
            raise ConnectorError(f"{contract.id}: no URL configured ({contract.url_env})", "unavailable")
        self.base = base

    def _headers(self) -> dict[str, str]:
        h = {}
        if self._secret:
            c = self.contract.credential
            h[c.header] = f"{c.scheme} {self._secret}".strip() if c.scheme else self._secret
        if self.trace_id:
            h[TRACE_HEADER] = self.trace_id
        return h

    def _scrub(self, text: str) -> None:
        if self._secret and self.contract.credential.mode == "service" and self._secret in text:
            raise ConnectorError(f"{self.contract.id} returned its own credential; response refused", "refused")

    async def request(self, method: str, path: str, *, json_body: Any = None,
                      params: Optional[dict] = None, headers: Optional[dict] = None) -> tuple[int, Any]:
        url = self.base + path
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=TRANSPORT) as client:
                r = await client.request(method, url, json=json_body, params=params,
                                         headers={**self._headers(), **(headers or {})})
        except httpx.TimeoutException:
            raise ConnectorError(f"{self.contract.id} timed out", "timeout") from None
        except httpx.HTTPError as exc:
            raise ConnectorError(f"{self.contract.id} unreachable: {type(exc).__name__}", "unavailable") from None
        self._scrub(r.text)
        try:
            body = r.json()
        except ValueError:
            body = {"text": r.text[:2000]}
        return r.status_code, body

    # ------------------------------------------------------------ MCP

    async def mcp_call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """initialize, notifications/initialized, tools/call; returns the
        result (content and structuredContent) or raises ConnectorError."""
        hdr = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": MCP_PROTOCOL}
        session: dict[str, str] = {}

        async def rpc(client, method, params=None, notify=False):
            msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
            if not notify:
                msg["id"] = next(_ids)
            r = await client.post(self.base, json=msg, headers={**self._headers(), **hdr, **session})
            self._scrub(r.text)
            if "mcp-session-id" in r.headers:
                session["Mcp-Session-Id"] = r.headers["mcp-session-id"]
            if notify:
                return None
            if r.status_code >= 400:
                raise ConnectorError(f"MCP {method}: HTTP {r.status_code}", "unavailable" if r.status_code >= 500
                                     else "refused", r.status_code)
            data = _mcp_body(r)
            if "error" in data:
                raise ConnectorError(f"MCP {method}: {data['error'].get('message', 'error')}", "error")
            return data.get("result", {})

        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=TRANSPORT) as client:
                init = await rpc(client, "initialize", {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                                                        "clientInfo": {"name": "nexa-tool-gateway",
                                                                       "version": "1.0.0"}})
                await rpc(client, "notifications/initialized", notify=True)
                result = await rpc(client, "tools/call", {"name": tool, "arguments": arguments})
        except httpx.TimeoutException:
            raise ConnectorError(f"{self.contract.id} timed out", "timeout") from None
        except httpx.HTTPError as exc:
            raise ConnectorError(f"{self.contract.id} unreachable: {type(exc).__name__}", "unavailable") from None
        if result.get("isError"):
            raise ConnectorError(f"MCP tool {tool} failed: {_mcp_text(result)[:300]}", "error")
        return {"server": (init.get("serverInfo") or {}).get("name"), "text": _mcp_text(result),
                "structured": result.get("structuredContent")}


def _mcp_body(r: httpx.Response) -> dict[str, Any]:
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except ValueError:
                    continue
        raise ConnectorError("MCP: no JSON-RPC message in the event stream", "error")
    try:
        return r.json()
    except ValueError:
        raise ConnectorError("MCP: response is not JSON", "error") from None


def _mcp_text(result: dict[str, Any]) -> str:
    return "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
