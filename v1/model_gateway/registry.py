"""Logical model registry.

Agents and services call a logical model name, never a concrete backend model.
Changing which model serves a logical name is a configuration change here, not
an application change. Every entry carries a version string that is stamped
into the response trace. See NEXA-ADR-0001.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterator, Optional


@dataclass(frozen=True)
class ModelEntry:
    logical: str          # name callers use, for example nexa-chat-default
    runtime: str          # ollama or openai
    backend_model: str    # concrete model id on that runtime
    version: str          # tracked version, stamped into every trace
    capability: str       # chat or embedding
    default_temperature: float = 0.4


# Initial registry mirrors the verified v0 models (v0 Stack Baseline section 3),
# now reached only through the gateway.
REGISTRY: dict[str, ModelEntry] = {
    "nexa-chat-default": ModelEntry(
        "nexa-chat-default", "ollama", "llama3.1:8b-instruct-q5_K_M",
        "llama3.1-8b-instruct-q5_K_M", "chat", 0.4),
    "nexa-chat-precise": ModelEntry(
        "nexa-chat-precise", "ollama", "llama3.1:8b-instruct-q5_K_M",
        "llama3.1-8b-instruct-q5_K_M", "chat", 0.1),
    "nexa-general": ModelEntry(
        "nexa-general", "openai", "gpt-4o",
        "gpt-4o", "chat", 0.3),
    "nexa-embed-default": ModelEntry(
        "nexa-embed-default", "ollama", "nomic-embed-text",
        "nomic-embed-text", "embedding", 0.0),
}


def resolve(logical: str) -> Optional[ModelEntry]:
    return REGISTRY.get(logical)


def list_models(capability: Optional[str] = None) -> Iterator[ModelEntry]:
    for entry in REGISTRY.values():
        if capability is None or entry.capability == capability:
            yield entry
