"""Request and response models for the Agent Runtime."""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel


class Message(BaseModel):
    role: str
    content: str


class RunRequest(BaseModel):
    messages: list[Message]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class RunResponse(BaseModel):
    agent: str
    model: str
    output: str
    x_nexa: Optional[dict[str, Any]] = None
