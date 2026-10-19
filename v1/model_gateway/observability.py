"""Trace id and structured logging.

Every response carries model, model version, runtime and a trace id. Logs are
one JSON object per line so they are easy to ship later. See NEXA-ARCH-001
section 15 and NEXA-ADR-0001. Sensitive payloads are never logged
(NEXA-GOV-001 section 12).
"""
from __future__ import annotations
import json
import sys
import time
import uuid


def new_trace_id() -> str:
    return uuid.uuid4().hex


def log_event(**fields) -> None:
    fields.setdefault("ts", round(time.time(), 3))
    sys.stdout.write(json.dumps(fields, default=str) + "\n")
    sys.stdout.flush()


class Timer:
    """Measures wall time in milliseconds for one call."""

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        self.ms = 0.0
        return self

    def __exit__(self, *exc) -> None:
        self.ms = round((time.perf_counter() - self._start) * 1000, 1)
