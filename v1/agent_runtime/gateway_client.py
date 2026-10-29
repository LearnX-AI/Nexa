"""Thin client for the Model Gateway (NEXA-ADR-0001).

The runtime calls the gateway with its own service token. All model traffic in
v1 goes through the gateway, never direct to Ollama or OpenAI. The caller's
trace id is forwarded so the gateway logs under the same id.
"""
from __future__ import annotations
from typing import Any, Optional
import httpx

from v1.observability import TRACE_HEADER

from . import config


class GatewayError(Exception):
    pass


async def chat(
    model: str,
    messages: list[dict[str, Any]],
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    trace_id: Optional[str] = None,
) -> dict[str, Any]:
    if not config.GATEWAY_TOKEN:
        raise GatewayError("NEXA_AGENT_GATEWAY_TOKEN not set")
    payload: dict[str, Any] = {"model": model, "messages": messages}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    headers = {"Authorization": f"Bearer {config.GATEWAY_TOKEN}"}
    if trace_id:
        headers[TRACE_HEADER] = trace_id
    url = f"{config.GATEWAY_URL}/v1/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=config.GATEWAY_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.RequestError as exc:
        raise GatewayError(f"gateway unreachable: {exc}")
    if resp.status_code != 200:
        raise GatewayError(f"gateway {resp.status_code}: {resp.text[:200]}")
    return resp.json()
