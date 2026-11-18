"""The workflow contract, nexa.workflow/1.0 (NEXA-M3-003, under NEXA-ADR-0003).

A workflow is a reviewed file in v1/agent_runtime/workflows/<id>.json. It names
the steps the orchestration engine runs, in what order and under what limits.
The engine, never an agent, decides what runs next: agents only return text,
and nothing an agent says can call another agent, skip a step, or raise a limit.

Step types

  agent       run one registered agent (by id, or by capability through the
              registry) as the calling user, with a prompt built from the
              input and earlier steps; per-step timeout, retries and fallback
              agents
  knowledge   a grounded answer from the Knowledge Service, as the calling user
  route       deterministic routing: the first rule whose condition holds on
              the input and earlier outputs picks the next step
  delegate    bounded autonomy: a coordinating agent chooses the next action
              from a fixed allowlist of agents (or DONE); the engine checks the
              choice, runs the delegate, and asks again, at most
              max_delegations times
  checkpoint  human in the loop: the run pauses until a person holding one of
              approver_roles approves or rejects it
  respond     a fixed, templated final output

The step graph must be acyclic (every run ends); the only repetition is inside
a delegate step, and it is bounded. Every run is also bounded by the
workflow's budget: steps, agent calls, wall time.

Templates are plain substitution, {{input.x}} or {{steps.<step>.<field>}}:
no expressions, nothing evaluated.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WORKFLOW_VERSION = "nexa.workflow/1.0"
WORKFLOWS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "workflows")
WF_ID = re.compile(r"^nexa-wf-[a-z0-9]+(?:-[a-z0-9]+)*$")
STEP_ID = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
TEMPLATE = re.compile(r"\{\{\s*([a-z_]+(?:\.[A-Za-z0-9_]+)*)\s*\}\}")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
END = "end"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- policies

class StepPolicy(Strict):
    timeout_s: float = Field(90.0, ge=1.0, le=600.0, description="One attempt of the step.")
    max_attempts: int = Field(1, ge=1, le=4)
    backoff_s: float = Field(1.0, ge=0.0, le=30.0)
    on_error: str = Field("fail", description="fail, or the id of the step to go to instead.")


class Budget(Strict):
    max_steps: int = Field(20, ge=1, le=100, description="Steps executed in one run, delegations included.")
    max_agent_calls: int = Field(10, ge=1, le=50)
    max_wall_s: float = Field(300.0, ge=5.0, le=1800.0, description="Running time, not time waiting for a person.")


# ---------------------------------------------------------------- conditions

class Condition(Strict):
    """True when the field's value matches. Exactly one test is given."""
    field: str = Field(description="input.<name> or steps.<step>.<field>")
    matches: Optional[str] = Field(None, description="Regular expression, case-insensitive, searched in the text.")
    equals: Optional[Union[str, int, float, bool]] = None
    one_of: Optional[list[Union[str, int, float, bool]]] = Field(None, alias="in")
    exists: Optional[bool] = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _one(self) -> "Condition":
        given = [x is not None for x in (self.matches, self.equals, self.one_of, self.exists)]
        if sum(given) != 1:
            raise ValueError("a condition has exactly one of matches, equals, in, exists")
        if self.matches is not None:
            re.compile(self.matches)
        return self


class Rule(Strict):
    when: list[Condition] = Field(min_length=1, description="All must hold.")
    goto: str
    reason: str = Field(min_length=3, description="Recorded in the trace when the rule is taken.")


# ---------------------------------------------------------------- steps

class _Step(Strict):
    id: str
    title: str = Field(min_length=3, max_length=120)
    policy: StepPolicy = Field(default_factory=StepPolicy)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not STEP_ID.match(v) or v == END:
            raise ValueError("step id must be lower snake case and not 'end'")
        return v


class AgentStep(_Step):
    type: Literal["agent"]
    agent: Optional[str] = Field(None, description="Agent id. Or give capability.")
    capability: Optional[str] = Field(None, description="Resolved through the registry at run time.")
    fallback_agents: list[str] = Field(default_factory=list)
    prompt: str = Field(min_length=3, max_length=6000)
    temperature: Optional[float] = Field(None, ge=0.0, le=1.0)
    max_tokens: Optional[int] = Field(None, ge=1, le=4096)
    next: str = END

    @model_validator(mode="after")
    def _target(self) -> "AgentStep":
        if (self.agent is None) == (self.capability is None):
            raise ValueError("an agent step names exactly one of agent or capability")
        return self


class KnowledgeStep(_Step):
    type: Literal["knowledge"]
    question: str = Field(min_length=3, max_length=2000)
    audience: Optional[Literal["student", "teacher"]] = None
    general: Optional[bool] = False
    next: str = END


class RouteStep(_Step):
    type: Literal["route"]
    rules: list[Rule] = Field(min_length=1)
    default: str


class DelegateStep(_Step):
    type: Literal["delegate"]
    coordinator: str = Field(description="Agent that chooses the next action.")
    delegates: dict[str, str] = Field(min_length=1, description="Allowed action name -> agent id.")
    instructions: str = Field(min_length=10, max_length=4000,
                              description="What the coordinator sees; the engine adds the allowed actions.")
    delegate_prompt: str = Field(min_length=3, max_length=4000,
                                 description="Sent to the delegate; {{task}} is the coordinator's instruction.")
    max_delegations: int = Field(2, ge=1, le=5)
    temperature: Optional[float] = Field(0.0, ge=0.0, le=1.0)
    next: str = END

    @field_validator("delegates")
    @classmethod
    def _names(cls, v: dict[str, str]) -> dict[str, str]:
        for k in v:
            if not re.match(r"^[a-z][a-z0-9_]{0,30}$", k) or k == "done":
                raise ValueError(f"delegate action {k!r} must be lower snake case and not 'done'")
        return v


class CheckpointStep(_Step):
    type: Literal["checkpoint"]
    approver_roles: list[str] = Field(min_length=1)
    four_eyes: bool = Field(False, description="The person who started the run may not decide.")
    show: str = Field(min_length=3, max_length=6000, description="What the reviewer is shown (template).")
    expires_after_s: int = Field(7 * 24 * 3600, ge=60, le=30 * 24 * 3600)
    on_approve: str
    on_reject: str = END


class RespondStep(_Step):
    type: Literal["respond"]
    output: str = Field(min_length=1, max_length=8000)
    next: str = END


Step = Annotated[Union[AgentStep, KnowledgeStep, RouteStep, DelegateStep, CheckpointStep, RespondStep],
                 Field(discriminator="type")]


class Workflow(Strict):
    workflow_version: Literal["nexa.workflow/1.0"]
    id: str
    name: str = Field(min_length=3, max_length=80)
    version: str
    owner: str = Field(min_length=2)
    description: str = Field(min_length=10, max_length=600)
    pattern: Literal["deterministic", "bounded_multi_agent"]
    state: Literal["draft", "active", "retired"]
    allowed_roles: list[str] = Field(min_length=1)
    run_permission: str = "model.invoke"
    input: dict[str, Literal["text", "short_text"]] = Field(min_length=1,
                                                            description="Input field -> kind (text <= 4000 chars, "
                                                                        "short_text <= 200).")
    budget: Budget = Field(default_factory=Budget)
    start: str
    steps: list[Step] = Field(min_length=1)
    output: str = Field("{{last.output}}", description="The run's final output (template); last = the last step.")

    @field_validator("id")
    @classmethod
    def _wid(cls, v: str) -> str:
        if not WF_ID.match(v):
            raise ValueError("workflow id must be kebab case starting with 'nexa-wf-'")
        return v

    @field_validator("version")
    @classmethod
    def _ver(cls, v: str) -> str:
        if not SEMVER.match(v):
            raise ValueError("version must be MAJOR.MINOR.PATCH")
        return v

    @model_validator(mode="after")
    def _graph(self) -> "Workflow":
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step id")
        known = set(ids) | {END}
        if self.start not in ids:
            raise ValueError(f"start {self.start!r} is not a step")
        edges = {s.id: successors(s) for s in self.steps}
        for sid, outs in edges.items():
            for t in outs:
                if t not in known:
                    raise ValueError(f"step {sid!r} goes to unknown step {t!r}")
        # Acyclic, so every run ends.
        state: dict[str, int] = {}

        def visit(n: str, path: list[str]) -> None:
            if n == END:
                return
            if state.get(n) == 1:
                raise ValueError("the step graph has a cycle: " + " -> ".join(path + [n]))
            if state.get(n) == 2:
                return
            state[n] = 1
            for t in edges[n]:
                visit(t, path + [n])
            state[n] = 2
        visit(self.start, [])
        unreachable = set(ids) - set(state)
        if unreachable:
            raise ValueError(f"steps never reached from start: {sorted(unreachable)}")
        # Templates only refer to the input, earlier steps, or (in a delegate prompt) the task.
        for s in self.steps:
            for text in templates_of(s):
                for ref in TEMPLATE.findall(text):
                    head, _, rest = ref.partition(".")
                    if head == "input" and rest.split(".")[0] not in self.input:
                        raise ValueError(f"step {s.id!r}: {{{{{ref}}}}} is not an input field")
                    if head == "steps" and rest.split(".")[0] not in ids:
                        raise ValueError(f"step {s.id!r}: {{{{{ref}}}}} names an unknown step")
                    if head not in ("input", "steps", "task", "last", "review"):
                        raise ValueError(f"step {s.id!r}: {{{{{ref}}}}} is not a known template value")
        # A knowledge step answers with the user's own session, which is never
        # stored; after a checkpoint the run continues without it.
        after: set[str] = set()
        todo = [t for s in self.steps if isinstance(s, CheckpointStep) for t in successors(s)]
        while todo:
            n = todo.pop()
            if n == END or n in after:
                continue
            after.add(n)
            todo.extend(edges[n])
        late = sorted(s.id for s in self.steps if isinstance(s, KnowledgeStep) and s.id in after)
        if late:
            raise ValueError(f"knowledge steps cannot come after a checkpoint: {late}")
        if self.pattern == "bounded_multi_agent" and not any(isinstance(s, DelegateStep) for s in self.steps):
            raise ValueError("a bounded_multi_agent workflow has a delegate step")
        return self

    def step(self, sid: str) -> Step:
        return next(s for s in self.steps if s.id == sid)

    def agents(self) -> set[str]:
        out: set[str] = set()
        for s in self.steps:
            if isinstance(s, AgentStep):
                out |= {x for x in [s.agent, *s.fallback_agents] if x}
            if isinstance(s, DelegateStep):
                out |= {s.coordinator, *s.delegates.values()}
        return out

    def fingerprint(self) -> str:
        raw = json.dumps(self.model_dump(mode="json", by_alias=True), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def successors(s: Step) -> list[str]:
    if isinstance(s, RouteStep):
        return [r.goto for r in s.rules] + [s.default]
    if isinstance(s, CheckpointStep):
        return [s.on_approve, s.on_reject]
    outs = [s.next]
    if s.policy.on_error != "fail":
        outs.append(s.policy.on_error)
    return outs


def templates_of(s: Step) -> list[str]:
    if isinstance(s, AgentStep):
        return [s.prompt]
    if isinstance(s, KnowledgeStep):
        return [s.question]
    if isinstance(s, DelegateStep):
        return [s.instructions, s.delegate_prompt]
    if isinstance(s, CheckpointStep):
        return [s.show]
    if isinstance(s, RespondStep):
        return [s.output]
    return []


# ---------------------------------------------------------------- platform checks

def validate_platform(wf: Workflow, agent_ids: set[str], roles: dict[str, set[str]]) -> list[str]:
    """Rules that need the rest of NEXA: every agent is registered, every role
    exists and holds the run permission."""
    problems = []
    for a in sorted(wf.agents() - agent_ids):
        problems.append(f"agent {a!r} is not in the agent registry")
    for r in wf.allowed_roles:
        if r not in roles:
            problems.append(f"role {r!r} is not a NEXA role")
        elif wf.run_permission not in roles[r]:
            problems.append(f"role {r!r} does not hold {wf.run_permission!r}")
    for s in wf.steps:
        if isinstance(s, CheckpointStep):
            for r in s.approver_roles:
                if r not in roles:
                    problems.append(f"checkpoint {s.id!r}: role {r!r} is not a NEXA role")
    return problems


class WorkflowError(Exception):
    def __init__(self, path: str, problems: list[str]):
        self.path, self.problems = path, problems
        super().__init__(f"{path}: " + "; ".join(problems))


def parse(path: str) -> Workflow:
    with open(path) as fh:
        raw = json.load(fh)
    try:
        return Workflow.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
        errs = getattr(exc, "errors", None)
        raise WorkflowError(path, [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in errs()]
                            if errs else [str(exc)]) from None


def load_dir(directory: str = WORKFLOWS_DIR, *, agent_ids: Optional[set[str]] = None,
             roles: Optional[dict[str, set[str]]] = None) -> dict[str, Workflow]:
    found: dict[str, Workflow] = {}
    if not os.path.isdir(directory):
        return found
    for f in sorted(os.listdir(directory)):
        if not f.endswith(".json"):
            continue
        path = os.path.join(directory, f)
        wf = parse(path)
        if f != f"{wf.id}.json":
            raise WorkflowError(path, [f"file must be named {wf.id}.json"])
        if agent_ids is not None and roles is not None:
            problems = validate_platform(wf, agent_ids, roles)
            if problems:
                raise WorkflowError(path, problems)
        found[wf.id] = wf
    return found


def json_schema() -> dict[str, Any]:
    s = Workflow.model_json_schema(by_alias=True)
    s["title"] = "NEXA workflow (nexa.workflow/1.0)"
    return s


def main(argv: list[str]) -> int:
    if argv[:1] == ["schema"]:
        print(json.dumps(json_schema(), indent=2))
        return 0
    from v1.identity.rbac import ROLE_PERMISSIONS
    from .. import registry
    found = load_dir(argv[1] if len(argv) > 1 else WORKFLOWS_DIR, agent_ids=set(registry.AGENTS),
                     roles=ROLE_PERMISSIONS)
    for wf in found.values():
        print(f"ok  {wf.id} {wf.version} {wf.state} {wf.pattern}  {len(wf.steps)} steps  {wf.fingerprint()[:19]}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:] or ["validate"]))
