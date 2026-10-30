"""Request and response schemas for the API Gateway (NEXA-M1-002).

Defined here, not imported from the Agent Runtime, so the services stay
decoupled (NEXA-M1-001). These models drive the generated OpenAPI document.
"""
from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    status: int
    message: str
    trace_id: Optional[str] = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class AgentMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class RunRequest(BaseModel):
    messages: list[AgentMessage] = Field(min_length=1, max_length=50)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=4096)


class RunResponse(BaseModel):
    schema_version: str
    agent: str
    agent_version: Optional[str] = None
    model: str
    output: str
    trace_id: Optional[str] = None
    usage: Optional[dict[str, Any]] = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    x_nexa: Optional[dict[str, Any]] = None


class AgentInfo(BaseModel):
    name: str
    description: str
    model: str
    version: str
    deprecated: bool
    deprecated_since: Optional[str] = None
    replaced_by: Optional[str] = None
    required_permission: Optional[str] = None
    capabilities: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class AgentList(BaseModel):
    agents: list[AgentInfo]
