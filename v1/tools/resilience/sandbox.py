"""An isolated NEXA for resilience scenarios (NEXA-M3-009).

The real Agent Runtime, orchestration engine, message bus, policy decision
point, state store and Tool Gateway, in memory, with nothing reaching the
network: the model, the Knowledge Service and (when a scenario breaks it) the
Tool Gateway are replaced by scripted stand-ins that inject faults on cue.

Used by the scenarios (scenarios.py), the pytest suite and the
resilience_eval command; never touches a deployed registry or audit store.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from v1.identity import tokens
from v1.identity.principal import Principal

STUDENT = Principal("eval:student", ("student",), "user", school_id="eval-school")
TEACHER = Principal("eval:teacher-1", ("teacher",), "user", school_id="eval-school")
TEACHER2 = Principal("eval:teacher-2", ("teacher",), "user", school_id="eval-school")
OTHER_SCHOOL = Principal("eval:teacher-other", ("teacher",), "user", school_id="eval-other-school")


def token(p: Principal) -> str:
    return tokens.issue_token(p.subject, list(p.roles), "user", 900,
                              extra={"school_id": p.school_id} if p.school_id else None)


class Patches:
    """Set attributes for the length of a scenario, then put them back."""

    def __init__(self):
        self._undo: list[tuple[Any, str, Any]] = []

    def set(self, obj: Any, name: str, value: Any) -> None:
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def item(self, d: dict, key: Any, value: Any) -> None:
        had = key in d
        old = d.get(key)
        self._undo.append((d, key, (had, old)))
        d[key] = value

    def restore(self) -> None:
        while self._undo:
            obj, name, old = self._undo.pop()
            if isinstance(obj, dict):
                had, value = old
                if had:
                    obj[name] = value
                else:
                    obj.pop(name, None)
            else:
                setattr(obj, name, old)


class ModelFault(Exception):
    pass


@dataclass
class Model:
    """A scripted Model Gateway.

    coordinator   the coordinator's replies, in order (dict -> JSON, str as is)
    expression    what the curriculum tutor writes when asked for an expression
    intent        what the assistant's classifier answers (NEXA-M3-010): a word,
                  or a callable message -> word
    answer        everything else (an explanation, a worked example)
    faults        call number (1-based) -> "unavailable" | "timeout" | "empty" | "garbage"
    always        a fault applied to every call
    slow_s        latency added to every call
    """
    coordinator: list[Any] = field(default_factory=list)
    expression: str = "500*4%*3"
    intent: Any = "curriculum"
    answer: str = "A clear explanation for Grade 11 students."
    faults: dict[int, str] = field(default_factory=dict)
    always: Optional[str] = None
    slow_s: float = 0.0
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def chat(self, model, messages, temperature=None, max_tokens=None, trace_id=None):
        n = len(self.calls) + 1
        prompt = messages[-1]["content"]
        role = "coordinator" if "Reply with only one JSON object" in prompt else \
            "expression" if "Reply with the expression only" in prompt else \
            "intent" if "Reply with one word: curriculum, general or smalltalk" in prompt else "answer"
        self.calls.append({"n": n, "model": model, "role": role, "prompt": prompt})
        fault = self.faults.get(n) or self.always
        if self.slow_s:
            await asyncio.sleep(self.slow_s)
        if fault == "unavailable":
            from v1.agent_runtime.gateway_client import GatewayError
            raise GatewayError("model backend unavailable (injected)", "unavailable")
        if fault == "timeout":
            await asyncio.sleep(3600)
        if fault == "empty":
            text = ""
        elif fault == "garbage":
            text = "I think maybe something like the explain thing would be good?"
        elif role == "coordinator":
            k = prompt.count("\nTask: ")
            c = self.coordinator[k] if k < len(self.coordinator) else {"action": "done"}
            text = c if isinstance(c, str) else json.dumps(c)
        elif role == "expression":
            text = self.expression
        elif role == "intent":
            msg = prompt.rsplit("Message: ", 1)[-1]
            text = self.intent(msg) if callable(self.intent) else self.intent
        else:
            text = self.answer
        return {"choices": [{"message": {"content": text}}], "x_nexa": {"model": model},
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}}

    def by_role(self, role: str) -> int:
        return sum(1 for c in self.calls if c["role"] == role)


@dataclass
class Knowledge:
    """The Knowledge Service behind the knowledge_answer tool.

    status   grounded | grounded_weak | no_evidence, or a callable question -> status
    fail     "unavailable" (connection refused) | "error" (HTTP 500) | None
    """
    status: Any = "grounded"
    fail: Optional[str] = None
    calls: list[str] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        q = body.get("question", "")
        self.calls.append(q)
        if self.fail == "unavailable":
            raise httpx.ConnectError("knowledge service down (injected)")
        if self.fail == "error":
            return httpx.Response(500, json={"detail": "internal error (injected)"})
        st = self.status(q) if callable(self.status) else self.status
        grounded = st in ("grounded", "grounded_weak")
        return httpx.Response(200, json={
            "status": st, "confidence": "high" if st == "grounded" else ("low" if grounded else "none"),
            "answer": "From the curriculum: chloroplasts capture light [S1]." if grounded else
            "The approved curriculum does not cover this question.",
            "evidence": {"level": "strong" if st == "grounded" else ("weak" if grounded else "none")},
            "citations": [{"marker": "S1", "used": True, "provenance": {
                "chunk_id": "eval-chunk-1", "title": "Biology Grade 11 Student Guide", "page_label": "12"}}]
            if grounded else []})


# The safety invariants found by the last World to close (the runner reads it).
LAST_VIOLATIONS: list[str] = []


class World:
    """One isolated NEXA. Use as a context manager. On close, before it is torn
    down, the safety invariants are checked on every run it started."""

    def __init__(self, model: Optional[Model] = None, knowledge: Optional[Knowledge] = None):
        self.model = model or Model()
        self.knowledge = knowledge or Knowledge()
        self.p = Patches()
        self.runs: list[str] = []

    def __enter__(self) -> "World":
        from v1.agent_runtime import config as rt_config, gateway_client, presence, registry, tools
        from v1.agent_runtime.orchestration import engine
        from v1.agent_runtime.policy import pdp
        from v1.tool_gateway import connectors, gateway, policy as tg_policy
        from v1.tool_gateway.main import app as tool_app
        registry.reload(":memory:")
        reg = registry.REGISTRY
        reg.set_state("nexa-curriculum-tutor", "0.2.0", "approved", "eval:admin", approved_by="Resilience evaluation")
        reg.set_state("nexa-curriculum-tutor", "0.2.0", "active", "eval:admin")
        pdp.load()
        engine.load_workflows()
        gateway.load(audit_path=":memory:")
        tg_policy.LIMITER.reset()
        engine._TASKS.clear()
        self.p.set(connectors, "TRANSPORT", httpx.MockTransport(self.knowledge))
        self.p.set(tools, "TRANSPORT", httpx.ASGITransport(app=tool_app))
        self.p.set(rt_config, "GATEWAY_TOKEN", tokens.issue_token("svc:agent-runtime", ["service"], "service", 900))
        self.p.set(gateway_client, "chat", self.model.chat)
        kn = self.knowledge

        async def knowledge_step(url, body, auth, trace_id, timeout):
            # The knowledge step of nexa-wf-student-question calls the
            # Knowledge Service directly (not through a tool).
            resp = kn(httpx.Request("POST", url, json=body))
            return resp.json() if resp.status_code == 200 else {"_status": resp.status_code}
        self.p.set(engine, "knowledge_answer", knowledge_step)

        async def deps():
            return {k: {"ok": True} for k in ("model_gateway", "knowledge_service", "tool_gateway")} | \
                {"memory_service": {"ok": False}}
        self.p.set(presence, "dependency_checks", deps)
        asyncio.run(presence.beat())
        return self

    def __exit__(self, *exc) -> None:
        from v1.agent_runtime import registry
        from v1.agent_runtime.orchestration import engine
        from v1.agent_runtime.policy import pdp
        from v1.tool_gateway import gateway
        from . import invariants
        for t in list(engine._TASKS):
            t.cancel()
        LAST_VIOLATIONS.clear()
        try:
            LAST_VIOLATIONS.extend(invariants.check_all(self.runs))
        except Exception as e:  # noqa: BLE001
            LAST_VIOLATIONS.append(f"invariants could not be checked: {type(e).__name__}: {e}")
        self.p.restore()
        registry.reload(":memory:")
        pdp.load()
        engine.load_workflows()
        gateway.load(audit_path=":memory:")

    # ------------------------------------------------------------ helpers

    def workflow(self, wf_id: str, **update) -> None:
        """Change a workflow for this scenario only (e.g. a shorter timeout)."""
        from v1.agent_runtime.orchestration import engine
        self.p.item(engine.WORKFLOWS, wf_id, engine.WORKFLOWS[wf_id].model_copy(update=update))

    def step(self, wf_id: str, step_id: str, **update) -> None:
        from v1.agent_runtime.orchestration import engine
        wf = engine.WORKFLOWS[wf_id]
        steps = [s.model_copy(update=update) if s.id == step_id else s for s in wf.steps]
        self.workflow(wf_id, steps=steps)

    def start(self, wf_id: str, who: Principal, inputs: dict[str, Any], **kw) -> dict[str, Any]:
        from v1.agent_runtime.orchestration import engine
        r = asyncio.run(engine.start(wf_id, who, inputs, auth=f"Bearer {token(who)}", **kw))
        self.runs.append(r["run_id"])
        return r

    def decide(self, run_id: str, who: Principal, decision: str = "approve", comment: str = "") -> dict[str, Any]:
        from v1.agent_runtime.orchestration import engine
        return asyncio.run(engine.decide(run_id, who, decision, comment))

    def trace(self, run_id: str) -> list[dict[str, Any]]:
        from v1.agent_runtime.orchestration import engine
        return engine.trace_of(run_id)

    def kinds(self, run_id: str) -> list[str]:
        return [t["kind"] for t in self.trace(run_id)]

    def path(self, run_id: str) -> list[str]:
        return [t["step"] for t in self.trace(run_id) if t["kind"] == "step.started"]


def run_safely(fn: Callable[[], Any]) -> tuple[Any, Optional[str]]:
    """Run a scenario action, returning (result, error)."""
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001 - scenarios assert on refusals
        detail = getattr(exc, "detail", None) or str(exc)
        return None, f"{type(exc).__name__}({getattr(exc, 'status_code', '')}): {detail}"


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<!\d)(?:\+?675[ -]?)?7\d{3}[ -]?\d{4}(?!\d)")
