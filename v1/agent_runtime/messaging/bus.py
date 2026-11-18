"""The message bus of the orchestration layer (NEXA-M3-005).

Every request to an agent or a tool, every result or error, and every event
of a run passes through here. The bus:

  - validates the envelope and its payload (contract.py) and refuses anything
    that breaks the routing rules: an agent never addresses an agent
  - refuses a request past its deadline, and discards a late result
  - deduplicates by idempotency key: the same key with the same payload is
    done once and its result returned again; with a different payload it is
    a conflict
  - records every message in order (messages table, per correlation id) so a
    run's whole conversation can be read back and checked

Handlers are the Agent Runtime (an agent run) and the Tool Gateway client
(a tool call): the bus calls them, they never call each other.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Optional

from v1.observability import audit_event

from . import contract as mc

# The tables (messages, message_idempotency) are created with the registry's (store.py).



class BusError(Exception):
    def __init__(self, code: str, reason: str, status: int = 409):
        self.code, self.reason, self.status = code, reason, status
        super().__init__(reason)


class Delivered:
    """What a handler returns: the result payload, or an error."""
    def __init__(self, payload: Optional[dict[str, Any]] = None, error: Optional[mc.ErrorInfo] = None,
                 sender_version: Optional[str] = None):
        self.payload, self.error, self.sender_version = payload, error, sender_version


Handler = Callable[[mc.Message, float], Awaitable[Delivered]]


def _store():
    from .. import registry
    if registry.REGISTRY is None:
        raise BusError("unavailable", "agent registry not open", 503)
    return registry.REGISTRY.store


def record(m: mc.Message, status: Optional[str] = None) -> mc.Message:
    """Validate (again: the envelope may have been built elsewhere) and append."""
    problems = mc.rules(m)
    if problems:
        audit_event(action=f"message.refused:{m.type}", decision="deny", principal=m.sender.id,
                    reason="; ".join(problems), correlation_id=m.correlation_id)
        raise BusError("invalid", "; ".join(problems), 422)
    env = m.dump()
    with _store().tx() as db:
        seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE correlation_id = ?",
                         (m.correlation_id,)).fetchone()[0]
        db.execute("INSERT INTO messages (correlation_id, seq, message_id, kind, type, sender, receiver, in_reply_to, "
                   "causation_id, created_at, status, envelope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (m.correlation_id, seq, m.message_id, m.kind, m.type, f"{m.sender.kind}:{m.sender.id}",
                    f"{m.receiver.kind}:{m.receiver.id}" if m.receiver else None, m.in_reply_to, m.causation_id,
                    m.created_at, status, json.dumps(env, ensure_ascii=False, default=str)))
    return m


def conversation(correlation_id: str) -> list[dict[str, Any]]:
    rows = _store().query("SELECT seq, status, envelope FROM messages WHERE correlation_id = ? ORDER BY seq",
                          (correlation_id,))
    return [{"seq": r["seq"], "status": r["status"], "message": json.loads(r["envelope"])} for r in rows]


def emit(type_: str, *, correlation_id: str, trace_id: str, sender: mc.Party, payload: dict[str, Any],
         causation_id: Optional[str] = None, payload_schema: Optional[str] = None,
         principal: Optional[mc.OnBehalfOf] = None) -> mc.Message:
    m = mc.make("event", type_, correlation_id=correlation_id, trace_id=trace_id, sender=sender, payload=payload,
                payload_schema=payload_schema, causation_id=causation_id, principal=principal)
    return record(m)


def _reply(req: mc.Message, d: Delivered) -> mc.Message:
    base = req.type.split(".")[0]
    sender = mc.Party(kind=req.receiver.kind, id=req.receiver.id, version=d.sender_version or req.receiver.version)
    common = dict(correlation_id=req.correlation_id, trace_id=req.trace_id, sender=sender, receiver=req.sender,
                  in_reply_to=req.message_id, causation_id=req.message_id, principal=req.principal)
    if d.error is not None:
        return mc.make("error", f"{base}.error", error=d.error, **common)
    return mc.make("result", f"{base}.result", payload=d.payload, **common)


async def request(req: mc.Message, handler: Handler) -> mc.Message:
    """Deliver one request; returns its result or error message (recorded).

    Idempotency: a key already completed with the same payload returns the
    recorded result as a new result message marked duplicate, without calling
    the handler; the same key with another payload is refused (conflict)."""
    if req.kind != "request":
        raise BusError("invalid", "only requests are delivered", 422)
    key, dig = req.idempotency_key, mc.digest(req.payload)
    st = _store()
    prior = st.query("SELECT * FROM message_idempotency WHERE idempotency_key = ?", (key,))
    if prior and prior[0]["payload_digest"] != dig:
        record(req, "refused")
        err = mc.ErrorInfo(code="conflict", retryable=False,
                           reason=f"idempotency key {key} was used for different work")
        return record(_reply(req, Delivered(error=err)), "conflict")
    if prior and prior[0]["result"]:
        record(req, "duplicate")
        cached = json.loads(prior[0]["result"])
        return record(_reply(req, Delivered(payload=cached["payload"], sender_version=cached.get("version"))),
                      "duplicate")
    now = time.time()
    if req.deadline <= now:
        record(req, "expired")
        err = mc.ErrorInfo(code="timeout", retryable=False, reason="the request's deadline had passed")
        return record(_reply(req, Delivered(error=err)), "expired")
    record(req, "delivered")
    if not prior:
        with st.tx() as db:
            db.execute("INSERT OR IGNORE INTO message_idempotency (idempotency_key, payload_digest, request_id) "
                       "VALUES (?,?,?)", (key, dig, req.message_id))
    try:
        d = await asyncio.wait_for(handler(req, req.deadline - now), timeout=max(0.001, req.deadline - now))
    except asyncio.TimeoutError:
        d = Delivered(error=mc.ErrorInfo(code="timeout", retryable=True,
                                         reason=f"no result within {req.deadline - now:.0f}s"))
    status = "ok"
    if d.error is None and time.time() > req.deadline:
        status = "late"         # recorded, but the orchestrator gets a timeout
        record(_reply(req, d), "late")
        d = Delivered(error=mc.ErrorInfo(code="timeout", retryable=True, reason="the result arrived after the deadline"))
    reply = record(_reply(req, d), status if d.error is None else "error")
    if d.error is None:
        with st.tx() as db:
            db.execute("UPDATE message_idempotency SET result = ?, completed_at = ? WHERE idempotency_key = ?",
                       (json.dumps({"payload": d.payload, "version": d.sender_version}, ensure_ascii=False),
                        time.time(), key))
    return reply


def check_conversation(correlation_id: str) -> dict[str, Any]:
    """Validate a recorded conversation against the contract: every envelope
    and payload, every reply answering a recorded request from its receiver,
    every causation id pointing to an earlier message, and no agent-to-agent
    message. Returns counts and problems (empty: valid)."""
    msgs = conversation(correlation_id)
    seen: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    counts: dict[str, int] = {}
    for row in msgs:
        raw = row["message"]
        try:
            m = mc.Message.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"#{row['seq']}: invalid envelope: {str(exc)[:200]}")
            continue
        counts[m.kind] = counts.get(m.kind, 0) + 1
        if m.schema_ != mc.MESSAGE_SCHEMA:
            problems.append(f"#{row['seq']}: schema {m.schema_}")
        if m.causation_id and m.causation_id not in seen:
            problems.append(f"#{row['seq']}: causation {m.causation_id} is not an earlier message")
        if m.kind in ("result", "error"):
            req = seen.get(m.in_reply_to or "")
            if req is None or req["kind"] != "request":
                problems.append(f"#{row['seq']}: answers no recorded request")
            elif (req["receiver"]["kind"], req["receiver"]["id"]) != (m.sender.kind, m.sender.id):
                problems.append(f"#{row['seq']}: answered by someone other than the request's receiver")
        if m.sender.kind == "agent" and m.receiver and m.receiver.kind == "agent":
            problems.append(f"#{row['seq']}: agent-to-agent message")
        seen[m.message_id] = raw
    return {"correlation_id": correlation_id, "messages": len(msgs), "counts": counts, "problems": problems,
            "valid": not problems}
