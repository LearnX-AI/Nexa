"""Logical model registry.

Agents and services call a logical model name, never a concrete backend model.
Changing which model serves a logical name is a configuration change, not an
application change: point NEXA_MODEL_CONFIG at a JSON file (see
models.example.json) and restart the gateway. Without that file the built-in
defaults below are used. Every entry carries a version string that is stamped
into the response trace. Callers may pin a version with "name@version" and may
use an alias. See NEXA-ADR-0001.
"""
from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Iterator, Optional

KNOWN_RUNTIMES = {"ollama", "openai", "vllm", "nim"}
KNOWN_CAPABILITIES = {"chat", "embedding"}


class RegistryError(ValueError):
    """Raised when a model config file is invalid. The gateway refuses to start."""


@dataclass(frozen=True)
class ModelEntry:
    logical: str          # name callers use, for example nexa-chat-default
    runtime: str          # ollama, openai, vllm or nim
    backend_model: str    # concrete model id on that runtime
    version: str          # tracked version, stamped into every trace
    capability: str       # chat or embedding
    default_temperature: float = 0.4
    aliases: tuple = ()   # other names that resolve to this entry


# Defaults mirror the verified v0 models (v0 Stack Baseline section 3), now
# reached only through the gateway.
DEFAULT_MODELS: tuple = (
    ModelEntry("nexa-chat-default", "ollama", "llama3.1:8b-instruct-q5_K_M",
               "llama3.1-8b-instruct-q5_K_M", "chat", 0.4),
    ModelEntry("nexa-chat-precise", "ollama", "llama3.1:8b-instruct-q5_K_M",
               "llama3.1-8b-instruct-q5_K_M", "chat", 0.1),
    ModelEntry("nexa-general", "openai", "gpt-4o", "gpt-4o", "chat", 0.3),
    ModelEntry("nexa-embed-default", "ollama", "nomic-embed-text",
               "nomic-embed-text", "embedding", 0.0),
)

REGISTRY: dict[str, ModelEntry] = {}
_ALIASES: dict[str, str] = {}


def _install(entries) -> None:
    """Replace the live registry in place so existing references stay valid."""
    REGISTRY.clear()
    _ALIASES.clear()
    for e in entries:
        REGISTRY[e.logical] = e
        for alias in e.aliases:
            _ALIASES[alias] = e.logical


def parse_models(raw: dict) -> list[ModelEntry]:
    entries: list[ModelEntry] = []
    seen: set[str] = set()
    for i, item in enumerate(raw.get("models", [])):
        try:
            entry = ModelEntry(
                logical=item["logical"],
                runtime=item["runtime"],
                backend_model=item["backend_model"],
                version=item["version"],
                capability=item["capability"],
                default_temperature=float(item.get("default_temperature", 0.4)),
                aliases=tuple(item.get("aliases", ())),
            )
        except KeyError as exc:
            raise RegistryError(f"models[{i}] is missing field {exc}") from exc
        if entry.runtime not in KNOWN_RUNTIMES:
            raise RegistryError(f"{entry.logical}: unknown runtime {entry.runtime!r}")
        if entry.capability not in KNOWN_CAPABILITIES:
            raise RegistryError(f"{entry.logical}: unknown capability {entry.capability!r}")
        for name in (entry.logical, *entry.aliases):
            if name in seen:
                raise RegistryError(f"duplicate model name or alias: {name}")
            seen.add(name)
        entries.append(entry)
    if not entries:
        raise RegistryError("model config defines no models")
    return entries


def reload(path: Optional[str] = None) -> None:
    """Load the registry from a JSON file, or restore defaults when path is empty."""
    if not path:
        _install(DEFAULT_MODELS)
        return
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        raise RegistryError(f"cannot read model config {path}: {exc}") from exc
    _install(parse_models(raw))


def resolve(name: str) -> Optional[ModelEntry]:
    """Resolve a logical name or alias, optionally pinned as name@version."""
    base, _, version = name.partition("@")
    entry = REGISTRY.get(base) or REGISTRY.get(_ALIASES.get(base, ""))
    if entry is not None and version and entry.version != version:
        return None
    return entry


def list_models(capability: Optional[str] = None) -> Iterator[ModelEntry]:
    for entry in REGISTRY.values():
        if capability is None or entry.capability == capability:
            yield entry


_install(DEFAULT_MODELS)
