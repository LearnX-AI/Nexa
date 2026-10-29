"""OpenAI-compatible request models. Responses are built as plain dicts in
main.py so they match the OpenAI shape plus an x_nexa trace block.
See NEXA-ADR-0001.
"""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: bool = False  # streaming is a later step; non streaming for now


class EmbeddingsRequest(BaseModel):
    model: str
    input: Any  # a string or a list of strings
