"""Policy checks of the Tool Gateway (NEXA-M3-004).

Each call is checked in this order, and the first failure decides:

  1. caller        only the Agent Runtime (a service identity holding
                   tools.invoke) may call; it presents the user's own token
  2. tool          exists and is active
  3. allowlist     the agent is on the tool's allowed_agents
  4. permission    the user holds the tool's permission
  5. scopes        each scope asked for is the tool's, granted to the agent,
                   and open to one of the user's roles
  6. arguments     match the tool's input schema
  7. data          personal data never leaves NEXA (data.leaves_nexa tools)
  8. rate          per user and per tool, per minute

Every decision, allowed or refused, is written to the audit trail.
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Any, Optional

from .contract import ToolContract


class PolicyError(Exception):
    def __init__(self, status_code: int, check: str, detail: str):
        self.status_code, self.check, self.detail = status_code, check, detail
        super().__init__(detail)


# ---------------------------------------------------------------- arguments

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def validate_args(schema: dict[str, Any], args: Any, path: str = "arguments") -> list[str]:
    """The subset of JSON Schema the contracts use: type, properties,
    required, additionalProperties false, enum, maxLength, minLength,
    pattern, minimum, maximum, maxItems, items."""
    problems: list[str] = []
    t = schema.get("type")
    if t:
        py = _TYPES[t]
        ok = isinstance(args, py) and not (t in ("integer", "number") and isinstance(args, bool))
        if not ok:
            return [f"{path}: must be {t}"]
    if "enum" in schema and args not in schema["enum"]:
        problems.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(args, str):
        if len(args) > schema.get("maxLength", 10 ** 9):
            problems.append(f"{path}: longer than {schema['maxLength']} characters")
        if len(args) < schema.get("minLength", 0):
            problems.append(f"{path}: shorter than {schema['minLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], args):
            problems.append(f"{path}: does not match the expected form")
    if isinstance(args, (int, float)) and not isinstance(args, bool):
        if "minimum" in schema and args < schema["minimum"]:
            problems.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and args > schema["maximum"]:
            problems.append(f"{path}: above {schema['maximum']}")
    if isinstance(args, list):
        if len(args) > schema.get("maxItems", 10 ** 9):
            problems.append(f"{path}: more than {schema['maxItems']} items")
        for i, x in enumerate(args):
            problems += validate_args(schema.get("items", {}), x, f"{path}[{i}]")
    if isinstance(args, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in args:
                problems.append(f"{path}.{k}: required")
        for k, v in args.items():
            if k not in props:
                if schema.get("additionalProperties", True) is False:
                    problems.append(f"{path}.{k}: not allowed")
                continue
            problems += validate_args(props[k], v, f"{path}.{k}")
    return problems


# ---------------------------------------------------------------- personal data

PII = {
    "email address": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone number": re.compile(r"(?<![\w.])(?:\+?675[ -]?)?(?:7\d{3}[ -]?\d{4}|\d{3}[ -]?\d{4})(?![\w])"),
    "card number": re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"),
}


def find_pii(value: Any) -> list[str]:
    found: set[str] = set()

    def walk(v):
        if isinstance(v, str):
            for name, rx in PII.items():
                if rx.search(v):
                    found.add(name)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(value)
    return sorted(found)


# ---------------------------------------------------------------- scopes

def resolve_scopes(tool: ToolContract, requested: Optional[list[str]], granted: list[str],
                   roles: tuple[str, ...]) -> list[str]:
    scopes = list(requested) if requested else [s for s in tool.default_scopes if s in granted] or \
        list(tool.default_scopes)
    for s in scopes:
        if s not in tool.scopes:
            raise PolicyError(403, "scopes", f"scope {s!r} is not a scope of {tool.id}")
        if s not in granted:
            raise PolicyError(403, "scopes", f"scope {s!r} is not granted to this agent")
        if not set(roles) & set(tool.scopes[s].roles):
            raise PolicyError(403, "scopes", f"scope {s!r} is not open to this user's role")
    return scopes


# ---------------------------------------------------------------- rate limits

class RateLimiter:
    """Sliding one-minute windows, in memory (one gateway process per host)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._hits: dict[tuple, deque] = {}

    def check(self, key: tuple, limit: int, now: Optional[float] = None) -> Optional[float]:
        """None if allowed (and counted); otherwise seconds until a slot frees."""
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= 60.0:
                q.popleft()
            if len(q) >= limit:
                return 60.0 - (now - q[0])
            q.append(now)
            return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


LIMITER = RateLimiter()


def rate_check(tool: ToolContract, user: str) -> None:
    wait = LIMITER.check(("tool", tool.id), tool.rate_limit.per_tool_per_minute)
    if wait is not None:
        raise PolicyError(429, "rate", f"{tool.id} is at its limit of {tool.rate_limit.per_tool_per_minute} "
                                       f"calls a minute; retry in {wait:.0f}s")
    wait = LIMITER.check(("user", tool.id, user), tool.rate_limit.per_user_per_minute)
    if wait is not None:
        raise PolicyError(429, "rate", f"limit of {tool.rate_limit.per_user_per_minute} {tool.id} calls a minute "
                                       f"per user; retry in {wait:.0f}s")
