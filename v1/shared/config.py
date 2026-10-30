"""Configuration and secrets helpers (NEXA-M1-006).

The pattern for every service:
- Non-secret defaults live in the service config with safe dev defaults.
- Secrets have no default. They are required and come from the environment,
  loaded by systemd from a root-owned EnvironmentFile (never from source).
- At startup each service calls preflight() with the names it requires. If any
  required variable is missing or blank, the service refuses to start.

This keeps DEV, QA, UAT and PROD differing only by their environment files, with
no environment-specific values in code.
"""
from __future__ import annotations
import os
from typing import Iterable


class ConfigError(RuntimeError):
    pass


def missing(names: Iterable[str]) -> list[str]:
    """Return the names whose environment value is unset or blank."""
    return [n for n in names if not os.getenv(n, "").strip()]


def preflight(service: str, required: Iterable[str]) -> None:
    """Raise ConfigError if any required variable is missing. Call at startup."""
    gaps = missing(required)
    if gaps:
        raise ConfigError(
            f"{service}: missing required configuration: {', '.join(sorted(gaps))}"
        )
