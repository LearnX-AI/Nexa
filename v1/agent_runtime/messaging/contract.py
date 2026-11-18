"""The NEXA inter-agent message and event contract, nexa.message/1.0 (NEXA-M3-005).

Agents never talk to each other. Every exchange that involves an agent is a
message between the agent and the orchestrator (the orchestration engine, or
the Agent Runtime acting for a direct API call), carried by the message bus
(bus.py), which validates, routes, records and deduplicates it.

Kinds

  request  the orchestrator asks one agent (or one tool, for an agent) to do
           one piece of work; it has a receiver, a deadline, an attempt
           number and an idempotency key
  result   the receiver's answer to a request, addressed to the request's
           sender, with in_reply_to set to the request's message_id
  error    the receiver could not do it: a code from a closed list, whether
           a retry may help, and a short reason
  event    something that happened (a run started, a step completed, a
           checkpoint was decided): no receiver, never answered, recorded in
           order in the run's event log

Envelope (every message)

  schema           "nexa.message/1.0": the version of this envelope
  message_id       unique, "msg_..."
  kind, type       the kind above, and the message type, e.g. agent.task
  payload_schema   the version of the payload's own schema, e.g.
                   nexa.payload.agent.task/1
  correlation_id   the conversation: every message of one workflow run (or
                   one API request) carries the same id
  causation_id     the message that caused this one: a chain from the run's
                   start to every request, result and event
  in_reply_to      for result and error: the request answered
  trace_id         the distributed trace, shared with every service call
  sender           who sends it: {kind, id, version}; kind is orchestrator,
                   agent, tool, user or service
  receiver         who must handle it (request, result, error); none for events
  principal        the user on whose behalf the work is done: subject, roles
  permission       the permission the receiver checks for that user
  created_at       when it was made
  deadline         request: the time after which it must not be started and
                   its result is discarded; timeout_s gives the same as a span
  attempt,         request: this attempt and the most allowed; a retry is a
  max_attempts     new message with the same idempotency key
  idempotency_key  request: work with the same key and the same payload is
                   done once; its result is returned again to a repeat
  payload          the content, validated against payload_schema
  error            for error messages: code, retryable, reason

Routing rules (enforced by the bus, NEXA-M3-003 principle)

  - a request is sent only by the orchestrator, to an agent or a tool
  - a result or error is sent only by the receiver of the request it answers,
    back to that request's sender
  - an event is sent only by the orchestrator, the runtime or a service
  - no message from an agent is ever addressed to another agent

Failure semantics

  codes      timeout, unavailable, refused, invalid, conflict, budget,
             cancelled, internal
  retryable  timeout and unavailable may be retried by the orchestrator,
             with backoff, up to max_attempts, under the same idempotency
             key; the others are final
  delivery   at least once; idempotency keys make the effect at most once,
             so a run sees each piece of work done exactly once
  late       a result after its request's deadline is recorded but discarded
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

MESSAGE_SCHEMA = "nexa.message/1.0"
ID = re.compile(r"^[a-z]{2,4}_[A-Za-z0-9]{8,40}$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Party(Strict):
    kind: Literal["orchestrator", "agent", "tool", "user", "service"]
    id: str = Field(min_length=2, max_length=120)
    version: Optional[str] = Field(None, max_length=40)


class OnBehalfOf(Strict):
    subject: str = Field(min_length=2, max_length=200)
    roles: list[str] = Field(default_factory=list)


ErrorCode = Literal["timeout", "unavailable", "refused", "invalid", "conflict", "budget", "cancelled", "internal"]
RETRYABLE: set[str] = {"timeout", "unavailable"}


class ErrorInfo(Strict):
    code: ErrorCode
    retryable: bool
    reason: str = Field(max_length=1000)
    status: Optional[int] = None

    @model_validator(mode="after")
    def _retry(self) -> "ErrorInfo":
        if self.retryable and self.code not in RETRYABLE:
            raise ValueError(f"error code {self.code} is never retryable")
        return self


# ---------------------------------------------------------------- payloads (versioned on their own)

class AgentTask(Strict):
    """nexa.payload.agent.task/1: the orchestrator asks an agent for text."""
    role: Literal["agent", "coordinator", "delegate"]
    workflow: Optional[str] = None
    step: Optional[str] = None
    prompt: str = Field(min_length=1, max_length=20000)
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(None, ge=1, le=8192)


class AgentResult(Strict):
    """nexa.payload.agent.result/1: an agent's answer."""
    output: str = Field(max_length=100000)
    agent_version: Optional[str] = None
    manifest: Optional[str] = None
    model: Optional[str] = None
    attempts: Optional[int] = None
    latency_ms: int = Field(ge=0)


class ToolTask(Strict):
    """nexa.payload.tool.task/1: the orchestrator asks the Tool Gateway to run a
    tool for an agent (NEXA-M3-004)."""
    tool: str
    for_agent: str
    step: Optional[str] = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    scopes: Optional[list[str]] = None


class ToolResult(Strict):
    """nexa.payload.tool.result/1"""
    call_id: Optional[str] = None
    tool_version: Optional[str] = None
    scopes: list[str] = Field(default_factory=list)
    result: dict[str, Any]
    latency_ms: int = Field(ge=0)


class DelegationChoice(Strict):
    """nexa.payload.delegation.choice/1: what the orchestrator accepted (or
    refused) from a coordinator's result. An event: the coordinator proposes,
    only the orchestrator decides and sends the next request."""
    step: str
    decision: int = Field(ge=1)
    proposed: Optional[str] = None
    accepted: bool
    action: Optional[str] = None
    delegate: Optional[str] = None
    problem: Optional[str] = None


class RunEvent(Strict):
    """nexa.payload.workflow.event/1: a workflow event (run, step, route,
    checkpoint); the detail is the trace entry."""
    workflow: str
    run_id: str
    step: Optional[str] = None
    seq: int = Field(ge=1)
    detail: dict[str, Any] = Field(default_factory=dict)


PAYLOADS: dict[str, type[BaseModel]] = {
    "nexa.payload.agent.task/1": AgentTask,
    "nexa.payload.agent.result/1": AgentResult,
    "nexa.payload.tool.task/1": ToolTask,
    "nexa.payload.tool.result/1": ToolResult,
    "nexa.payload.delegation.choice/1": DelegationChoice,
    "nexa.payload.workflow.event/1": RunEvent,
    "nexa.payload.error/1": None,            # error messages carry ErrorInfo, no payload
}

# Message type -> (kind, payload schema). A type is never reused with another meaning.
TYPES: dict[str, tuple[str, str]] = {
    "agent.task": ("request", "nexa.payload.agent.task/1"),
    "agent.result": ("result", "nexa.payload.agent.result/1"),
    "agent.error": ("error", "nexa.payload.error/1"),
    "tool.task": ("request", "nexa.payload.tool.task/1"),
    "tool.result": ("result", "nexa.payload.tool.result/1"),
    "tool.error": ("error", "nexa.payload.error/1"),
    "delegation.choice": ("event", "nexa.payload.delegation.choice/1"),
}
EVENT_PREFIXES = ("run.", "step.", "route.", "checkpoint.", "delegate.", "agent.", "tool.", "knowledge.", "session.")


def event_type_ok(t: str) -> bool:
    return t in TYPES and TYPES[t][0] == "event" or (t not in TYPES and t.startswith(EVENT_PREFIXES))


class Message(Strict):
    schema_: Literal["nexa.message/1.0"] = Field(MESSAGE_SCHEMA, alias="schema")
    message_id: str
    kind: Literal["request", "result", "error", "event"]
    type: str = Field(pattern=r"^[a-z][a-z_]*(\.[a-z][a-z_]*)+$")
    payload_schema: str
    correlation_id: str = Field(min_length=6, max_length=80)
    causation_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    trace_id: str = Field(min_length=6, max_length=80)
    sender: Party
    receiver: Optional[Party] = None
    principal: Optional[OnBehalfOf] = None
    permission: Optional[str] = None
    created_at: float
    deadline: Optional[float] = None
    timeout_s: Optional[float] = Field(None, gt=0, le=3600)
    attempt: Optional[int] = Field(None, ge=1, le=10)
    max_attempts: Optional[int] = Field(None, ge=1, le=10)
    idempotency_key: Optional[str] = Field(None, min_length=8, max_length=200)
    payload: Optional[dict[str, Any]] = None
    error: Optional[ErrorInfo] = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _rules(self) -> "Message":
        problems = rules(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


def rules(m: Message) -> list[str]:
    """The envelope's rules, as a list of problems."""
    p: list[str] = []
    if not ID.match(m.message_id) or not m.message_id.startswith("msg_"):
        p.append("message_id must look like msg_<id>")
    if m.kind == "event":
        if not event_type_ok(m.type):
            p.append(f"{m.type} is not an event type")
        if m.receiver is not None:
            p.append("an event has no receiver")
        if m.sender.kind == "agent":
            p.append("agents do not send events")
    else:
        if m.type not in TYPES or TYPES[m.type][0] != m.kind:
            p.append(f"{m.type} is not a {m.kind} type")
        if m.receiver is None:
            p.append(f"a {m.kind} has a receiver")
    if m.type in TYPES and TYPES[m.type][1] != m.payload_schema:
        p.append(f"{m.type} carries {TYPES[m.type][1]}, not {m.payload_schema}")
    if m.kind == "request":
        if m.sender.kind != "orchestrator":
            p.append("only the orchestrator sends requests")
        for f in ("deadline", "timeout_s", "attempt", "max_attempts", "idempotency_key", "principal", "permission"):
            if getattr(m, f) is None:
                p.append(f"a request carries {f}")
        if m.attempt and m.max_attempts and m.attempt > m.max_attempts:
            p.append("attempt is more than max_attempts")
    if m.kind in ("result", "error"):
        if not m.in_reply_to:
            p.append(f"a {m.kind} names the request it answers (in_reply_to)")
        if m.receiver is not None and m.receiver.kind != "orchestrator":
            p.append(f"a {m.kind} goes back to the orchestrator")
    if m.sender.kind == "agent" and m.receiver is not None and m.receiver.kind == "agent":
        p.append("agents never message agents")
    if m.receiver is not None and m.receiver.kind == "agent" and m.sender.kind not in ("orchestrator",):
        p.append("only the orchestrator addresses an agent")
    if m.kind == "error":
        if m.error is None:
            p.append("an error message carries error")
        if m.payload is not None:
            p.append("an error message carries no payload")
    elif m.error is not None:
        p.append(f"a {m.kind} carries no error")
    model = PAYLOADS.get(m.payload_schema, False)
    if model is False:
        p.append(f"unknown payload schema {m.payload_schema}")
    elif model is not None:
        if m.payload is None:
            p.append(f"{m.type} carries a payload")
        else:
            try:
                model.model_validate(m.payload)
            except Exception as exc:  # noqa: BLE001
                errs = getattr(exc, "errors", None)
                p.extend(f"payload.{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in errs()) if errs \
                    else p.append(f"payload: {exc}")
    return p


def new_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def make(kind: str, type_: str, *, correlation_id: str, trace_id: str, sender: Party,
         receiver: Optional[Party] = None, payload: Optional[dict[str, Any]] = None,
         payload_schema: Optional[str] = None, **fields: Any) -> Message:
    schema = payload_schema or (TYPES[type_][1] if type_ in TYPES else "nexa.payload.workflow.event/1")
    return Message(message_id=new_id(), kind=kind, type=type_, payload_schema=schema, correlation_id=correlation_id,
                   trace_id=trace_id, sender=sender, receiver=receiver, payload=payload, created_at=time.time(),
                   **fields)


def digest(payload: Any) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                                 default=str).encode()).hexdigest()[:32]


def json_schema() -> dict[str, Any]:
    """The published schema: the envelope and every payload version."""
    s = Message.model_json_schema(by_alias=True)
    s["title"] = "NEXA message envelope (nexa.message/1.0)"
    s["x-nexa-types"] = {t: {"kind": k, "payload_schema": ps} for t, (k, ps) in TYPES.items()}
    s["x-nexa-event-prefixes"] = list(EVENT_PREFIXES)
    s["x-nexa-payloads"] = {name: (m.model_json_schema() if m else None) for name, m in PAYLOADS.items()}
    s["x-nexa-error-codes"] = {"codes": list(ErrorCode.__args__), "retryable": sorted(RETRYABLE)}
    return s
