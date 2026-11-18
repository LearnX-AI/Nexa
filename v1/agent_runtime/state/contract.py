"""Short-lived agent state: nexa.state/1.0 (NEXA-M3-006).

Three things that must not be confused:

  run state      the working state of one workflow run: the outputs of the
                 steps done so far, the routing and review decisions, the
                 counters. Checkpointed after every step, so a run can resume
                 after a restart. Lives as long as the run, then the state
                 retention period (purge.py).
  session state  a small key/value record one user owns, for handing context
                 from one run to the next ("last topic", "last question").
                 Expires after it is idle (ttl) and in any case after a
                 maximum age. Only workflows the user named may use it, and
                 only the keys each workflow declares.
  long-term      learner records and long-term memory (profiles, progress,
  memory         marks): NOT here. They belong to the LMS and the Memory
                 Service (port 8104, not built yet). State may carry a
                 reference to them (MemoryRef), never a copy.

Rules on any state (check_data):

  - JSON values only, at most 6 levels deep (run state 16), strings at most 8,000
    characters, and a total size limit (session 32 KB, run 512 KB)
  - no credentials: nothing that looks like a token or a bearer header
  - session state (what is handed from run to run) refuses learner-record
    fields (marks, grades, scores, attendance, date of birth, address, ...),
    which are referenced, not copied, and personal data (email addresses,
    phone numbers)
  - memory references only to the scopes the agent's manifest declares
    (memory.long_term_scopes); today no agent declares any, so none are
    accepted

Every record is serialised the same way (StateRecord): schema, kind, id,
owner, scope, a version that increases on every write (writes say which
version they expect: a stale write is a conflict, never a silent
overwrite), timestamps, expiry, the data and its digest. A digest that does
not match its data is refused on read: state is never resumed from a damaged
record.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

STATE_SCHEMA = "nexa.state/1.0"
LIMITS = {"session": 32 * 1024, "run_checkpoint": 512 * 1024}
MAX_DEPTH = {"session": 6, "run_checkpoint": 16}
MAX_STRING = 8000
SESSION_TTL_DEFAULT = 24 * 3600
SESSION_TTL_MAX = 7 * 24 * 3600
SESSION_MAX_AGE = 30 * 24 * 3600
SESSION_KEY = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

# Fields that make a value a learner record. Such data lives in the LMS or the
# Memory Service; state holds a MemoryRef to it.
LEARNER_RECORD_KEYS = {
    "mark", "marks", "grade_result", "grades", "score", "scores", "exam_result", "exam_results",
    "assessment", "assessments", "attendance", "report_card", "date_of_birth", "dob", "birthdate",
    "address", "home_address", "phone", "phone_number", "email", "guardian", "parent_contact",
    "student_number", "national_id", "disability", "medical",
}
CREDENTIAL = re.compile(r"(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})|(\bBearer\s+\S{12,})", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<!\d)(?:\+?675[ -]?)?7\d{3}[ -]?\d{4}(?!\d)")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryRef(Strict):
    """A pointer to long-term memory or a learner record, never its content."""
    service: Literal["nexa-memory", "lms"]
    scope: Literal["learner_profile", "class_progress", "lms_record"]
    subject: str = Field(min_length=2, max_length=200)
    key: str = Field(min_length=1, max_length=200)
    version: Optional[str] = Field(None, max_length=40)


class Owner(Strict):
    subject: str
    roles: list[str] = Field(default_factory=list)


class StateRecord(Strict):
    schema_: Literal["nexa.state/1.0"] = Field(STATE_SCHEMA, alias="schema")
    kind: Literal["session", "run_checkpoint"]
    id: str
    owner: Owner
    scope: dict[str, Any] = Field(default_factory=dict, description="session: workflows; run: workflow, version, "
                                                                     "fingerprint")
    version: int = Field(ge=1)
    created_at: float
    updated_at: float
    expires_at: Optional[float] = None
    data: dict[str, Any]
    digest: str

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


def digest(data: Any) -> str:
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


class StateError(Exception):
    def __init__(self, status_code: int, detail: Any):
        self.status_code, self.detail = status_code, detail
        super().__init__(str(detail))


def check_data(data: Any, kind: str, *, memory_scopes: frozenset[str] = frozenset()) -> list[str]:
    """Problems with a state value (empty: acceptable)."""
    problems: list[str] = []
    try:
        size = len(json.dumps(data, ensure_ascii=False))
    except (TypeError, ValueError):
        return ["state must be JSON"]
    if size > LIMITS[kind]:
        problems.append(f"state is {size} bytes, over the {LIMITS[kind]} byte limit for {kind}")

    def walk(v: Any, path: str, depth: int) -> None:
        if depth > MAX_DEPTH[kind]:
            problems.append(f"{path}: nested deeper than {MAX_DEPTH[kind]} levels")
            return
        if isinstance(v, dict):
            if "$memory" in v:
                try:
                    ref = MemoryRef.model_validate(v["$memory"])
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"{path}: invalid memory reference: {str(exc)[:120]}")
                    return
                if ref.scope not in memory_scopes:
                    problems.append(f"{path}: memory scope {ref.scope!r} is not declared by the agent's manifest")
                if len(v) != 1:
                    problems.append(f"{path}: a memory reference carries nothing but the reference")
                return
            for k, x in v.items():
                if not isinstance(k, str):
                    problems.append(f"{path}: keys are strings")
                    continue
                if kind == "session" and k.lower() in LEARNER_RECORD_KEYS:
                    problems.append(f"{path}.{k}: learner records are not state; store a memory reference")
                walk(x, f"{path}.{k}", depth + 1)
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]", depth + 1)
        elif isinstance(v, str):
            if len(v) > MAX_STRING:
                problems.append(f"{path}: longer than {MAX_STRING} characters")
            if CREDENTIAL.search(v):
                problems.append(f"{path}: looks like a credential; state never holds credentials")
            if kind == "session" and (EMAIL.search(v) or PHONE.search(v)):
                problems.append(f"{path}: personal data (email or phone) is not kept in session state")
        elif v is not None and not isinstance(v, (bool, int, float)):
            problems.append(f"{path}: not a JSON value")
    walk(data, "state", 0)
    return problems[:20]


def json_schema() -> dict[str, Any]:
    s = StateRecord.model_json_schema(by_alias=True)
    s["title"] = "NEXA agent state record (nexa.state/1.0)"
    s["x-nexa-memory-ref"] = MemoryRef.model_json_schema()
    s["x-nexa-limits"] = {"bytes": LIMITS, "max_depth": MAX_DEPTH, "max_string": MAX_STRING,
                          "session_ttl_default_s": SESSION_TTL_DEFAULT, "session_ttl_max_s": SESSION_TTL_MAX,
                          "session_max_age_s": SESSION_MAX_AGE}
    s["x-nexa-learner-record-keys"] = sorted(LEARNER_RECORD_KEYS)
    return s
