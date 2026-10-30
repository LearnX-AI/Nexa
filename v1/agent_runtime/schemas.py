"""Request and response models for the Agent Runtime (NEXA-ADR-0003).

These are the structured input and output envelopes. The response carries a
schema version, the trace id, the agent version and usage so a caller can rely
on a stable contract.
"""
from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field

ENVELOPE_VERSION = "1"


class Message(BaseModel):
    # Callers may send user and assistant turns only. The system prompt belongs
    # to the agent, so a client cannot override it by sending role "system".
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class RunRequest(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=50)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=4096)


class RunResponse(BaseModel):
    schema_version: str = ENVELOPE_VERSION
    agent: str
    agent_version: Optional[str] = None
    model: str
    output: str
    trace_id: Optional[str] = None
    usage: Optional[dict[str, Any]] = None
    # Tool calls made during the run. Empty in M1: tool use arrives with the
    # Tool Gateway (NEXA-ADR-0006).
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    x_nexa: Optional[dict[str, Any]] = None
