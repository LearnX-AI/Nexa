"""NEXA agent contract: the agent manifest, version nexa.agent/1.0 (NEXA-M3-001).

Every NEXA agent is described by one manifest file (JSON) in
v1/agent_runtime/agents/. The manifest is the contract between the agent's
owner, the Agent Runtime and the reviewers who approve it. It declares:

  identity      id, name, semantic version, owner
  purpose       what it is for, who it serves, what is out of scope
  capabilities  from a controlled vocabulary
  interface     the input and output envelopes it accepts and returns
  permissions   who may run it, and which knowledge it may read
  tools         each tool it may call, the permission and a call limit
  model         the logical gateway model, fallbacks and generation limits
  memory        what conversation and long-term state it may use
  safety        the policy it runs under and the rules it must keep
  runtime       timeout, retries, concurrency
  health        what must be up for the agent to be ready
  lifecycle     its state, approval record, deprecation and changelog

Two layers of checking:
  1. structure: the pydantic model below (exported as JSON Schema,
     nexa-agent-manifest-1.0.schema.json), so a manifest that is malformed,
     has an unknown field or a wrong type is rejected;
  2. platform rules (validate_platform): the model exists in the gateway, every
     permission and role exists in NEXA identity, every tool is registered,
     the lifecycle record is complete for the state, and audience, data access
     and safety agree with each other.

    python -m v1.agent_runtime.manifest validate v1/agent_runtime/agents/*.json
    python -m v1.agent_runtime.manifest schema > nexa-agent-manifest-1.0.schema.json
"""
from __future__ import annotations

import enum
import hashlib
import json
import os
import re
import sys
from datetime import date
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

MANIFEST_VERSION = "nexa.agent/1.0"
AGENTS_DIR = os.path.join(os.path.dirname(__file__), "agents")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
AGENT_ID = re.compile(r"^nexa-[a-z0-9]+(?:-[a-z0-9]+)*$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- vocabularies

class Capability(str, enum.Enum):
    """What an agent does. A new capability is added here, reviewed, and
    released as a minor version of the contract."""
    chat = "chat"                                   # open conversation within its purpose
    curriculum_answer = "curriculum_answer"         # answers grounded in approved curriculum, with citations
    general_answer = "general_answer"               # labelled answers from general knowledge
    explanation = "explanation"                     # step-by-step explanation of a concept
    calculation = "calculation"                     # worked calculations
    lesson_planning = "lesson_planning"             # lesson and unit plans for teachers
    assessment_generation = "assessment_generation" # questions, quizzes and marking guides
    summarisation = "summarisation"
    translation = "translation"                     # between approved languages
    course_outline = "course_outline"               # syllabus and course outlines


class Audience(str, enum.Enum):
    student = "student"
    teacher = "teacher"
    school_admin = "school_admin"
    national_admin = "national_admin"
    service = "service"


class LifecycleState(str, enum.Enum):
    draft = "draft"            # being written; not runnable
    review = "review"          # submitted for review; not runnable
    approved = "approved"      # approved, not yet switched on; not runnable
    active = "active"          # runnable
    suspended = "suspended"    # switched off for a problem; not runnable; can return to active
    deprecated = "deprecated"  # runnable until its sunset date, with a warning; a successor is named
    retired = "retired"        # permanently off; the manifest stays for the record


RUNNABLE = {LifecycleState.active, LifecycleState.deprecated}

# Allowed lifecycle transitions. Anything else is refused.
TRANSITIONS: dict[LifecycleState, set[LifecycleState]] = {
    LifecycleState.draft: {LifecycleState.review},
    LifecycleState.review: {LifecycleState.draft, LifecycleState.approved},
    LifecycleState.approved: {LifecycleState.active, LifecycleState.draft},
    LifecycleState.active: {LifecycleState.suspended, LifecycleState.deprecated},
    LifecycleState.suspended: {LifecycleState.active, LifecycleState.deprecated, LifecycleState.retired},
    LifecycleState.deprecated: {LifecycleState.retired, LifecycleState.active},
    LifecycleState.retired: set(),
}


def can_transition(old: LifecycleState, new: LifecycleState) -> bool:
    return new in TRANSITIONS[old]


# ---------------------------------------------------------------- sections

class Identity(Strict):
    id: str = Field(description="Stable id, lower-case, starts with 'nexa-'. Never reused.")
    name: str = Field(min_length=3, max_length=80, description="Display name.")
    version: str = Field(description="Semantic version of this agent (MAJOR.MINOR.PATCH).")
    owner: str = Field(min_length=2, description="Accountable team or person.")
    description: str = Field(min_length=10, max_length=500)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not AGENT_ID.match(v):
            raise ValueError("id must be lower-case kebab case starting with 'nexa-'")
        return v

    @field_validator("version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not SEMVER.match(v):
            raise ValueError("version must be semantic: MAJOR.MINOR.PATCH")
        return v


class Purpose(Strict):
    summary: str = Field(min_length=10, max_length=500)
    audience: list[Audience] = Field(min_length=1)
    in_scope: list[str] = Field(min_length=1)
    out_of_scope: list[str] = Field(min_length=1, description="What the agent must decline or hand off.")


class Interface(Strict):
    """The envelopes the agent accepts and returns. Version 1 of both is the
    Agent Runtime's RunRequest and RunResponse (schemas.py)."""
    input: Literal["nexa.run.request/1"] = "nexa.run.request/1"
    output: Literal["nexa.run.response/1"] = "nexa.run.response/1"
    accepts_course_context: bool = Field(False, description="Uses the learner's course grade and subject.")
    max_input_messages: int = Field(50, ge=1, le=50)
    output_includes_citations: bool = False


class KnowledgeAccess(Strict):
    access_classes: list[Literal["learner", "teacher_only", "restricted"]] = Field(default_factory=list)
    scopes: list[Literal["national", "provincial", "school", "lms_site"]] = Field(default_factory=list)


class Permissions(Strict):
    run_permission: str = Field("model.invoke", description="Permission a caller must hold to run the agent.")
    allowed_roles: list[str] = Field(min_length=1, description="Roles that may run it (each must hold run_permission).")
    knowledge: KnowledgeAccess = Field(default_factory=KnowledgeAccess)


class ToolGrant(Strict):
    name: str
    purpose: str = Field(min_length=5)
    permission: str = Field(description="Permission the caller must hold for this tool call.")
    max_calls_per_run: int = Field(3, ge=1, le=20)


class ModelRequirements(Strict):
    model: str = Field(description="Logical model name in the Model Gateway, never a backend.")
    fallbacks: list[str] = Field(default_factory=list)
    temperature: float = Field(0.3, ge=0.0, le=2.0)
    max_output_tokens: int = Field(700, ge=1, le=4096)
    min_context_tokens: int = Field(4096, ge=512)


class Memory(Strict):
    conversation: Literal["none", "request"] = Field(
        "request", description="request: only the messages sent with the run; none: single turn only.")
    long_term: Literal["none", "read", "read_write"] = "none"
    long_term_scopes: list[Literal["learner_profile", "class_progress"]] = Field(default_factory=list)
    retention_days: int = Field(0, ge=0, le=365)


class Safety(Strict):
    policy: str = Field(description="Safety policy id, for example nexa.safety.student/1.")
    serves_minors: bool
    require_citations_for_curriculum: bool = True
    general_answers: Literal["off", "labelled"] = "labelled"
    refuse: list[str] = Field(min_length=1, description="Request categories the agent must decline.")
    pii_in_prompts: Literal["forbidden"] = "forbidden"
    escalation: str = Field(min_length=5, description="Where a learner is pointed when the agent must decline.")


class Retry(Strict):
    max_attempts: int = Field(2, ge=1, le=5)
    backoff_s: float = Field(2.0, ge=0.0, le=60.0)
    retry_on: list[Literal["gateway_unavailable", "timeout"]] = Field(default_factory=lambda: ["gateway_unavailable"])


class Runtime(Strict):
    timeout_s: float = Field(60.0, ge=1.0, le=600.0, description="Whole run, all attempts included.")
    retry: Retry = Field(default_factory=Retry)
    max_concurrent_runs: int = Field(0, ge=0, description="0: limited only by the runtime's queue.")


class Health(Strict):
    requires: list[Literal["model_gateway", "knowledge_service", "tool_registry", "memory_service"]] = Field(
        default_factory=lambda: ["model_gateway"])


class ChangelogEntry(Strict):
    version: str
    date: date
    change: str = Field(min_length=3)


class Lifecycle(Strict):
    state: LifecycleState
    approved_by: Optional[str] = None
    approved_on: Optional[date] = None
    deprecated_on: Optional[date] = None
    sunset_on: Optional[date] = None
    replaced_by: Optional[str] = None
    changelog: list[ChangelogEntry] = Field(min_length=1)


class Prompt(Strict):
    system: str = Field(min_length=20, max_length=8000)


class AgentManifest(Strict):
    manifest_version: Literal["nexa.agent/1.0"]
    identity: Identity
    purpose: Purpose
    capabilities: list[Capability] = Field(min_length=1)
    interface: Interface = Field(default_factory=Interface)
    permissions: Permissions
    tools: list[ToolGrant] = Field(default_factory=list)
    model: ModelRequirements
    memory: Memory = Field(default_factory=Memory)
    safety: Safety
    runtime: Runtime = Field(default_factory=Runtime)
    health: Health = Field(default_factory=Health)
    lifecycle: Lifecycle
    prompt: Prompt

    @model_validator(mode="after")
    def _structural_rules(self) -> "AgentManifest":
        lc, ver = self.lifecycle, self.identity.version
        if lc.changelog[0].version != ver:
            raise ValueError("the first changelog entry must be the current version")
        if lc.state in (LifecycleState.approved, LifecycleState.active, LifecycleState.suspended,
                        LifecycleState.deprecated, LifecycleState.retired) and not (lc.approved_by and lc.approved_on):
            raise ValueError(f"a {lc.state.value} agent must record approved_by and approved_on")
        if lc.state == LifecycleState.deprecated and not (lc.deprecated_on and lc.sunset_on):
            raise ValueError("a deprecated agent must record deprecated_on and sunset_on")
        if lc.sunset_on and lc.deprecated_on and lc.sunset_on < lc.deprecated_on:
            raise ValueError("sunset_on is before deprecated_on")
        names = [t.name for t in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("a tool is granted twice")
        if Capability.curriculum_answer in self.capabilities and not self.interface.output_includes_citations:
            raise ValueError("an agent with curriculum_answer must return citations")
        if self.memory.long_term == "none" and (self.memory.long_term_scopes or self.memory.retention_days):
            raise ValueError("long_term none cannot declare memory scopes or retention")
        if self.memory.long_term != "none" and "memory_service" not in self.health.requires:
            raise ValueError("an agent using long-term memory must require memory_service in health")
        if self.tools and "tool_registry" not in self.health.requires:
            raise ValueError("an agent with tools must require tool_registry in health")
        return self

    # ------------------------------------------------------------ derived

    @property
    def id(self) -> str:
        return self.identity.id

    @property
    def runnable(self) -> bool:
        return self.lifecycle.state in RUNNABLE

    def fingerprint(self) -> str:
        """sha256 of the canonical manifest: what was approved is what runs."""
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------- safety policies

# The policies an agent may run under. The Safety service (later in M3) will
# own them; until then they are defined here and referenced by id.
SAFETY_POLICIES: dict[str, str] = {
    "nexa.safety.student/1": "Learners in PNG senior secondary schools, minors included: age-appropriate "
                             "content only; no personal data in prompts; declines harmful, adult, violent, "
                             "self-harm, cheating-on-assessment and political-persuasion requests and points "
                             "the learner to a teacher; curriculum answers must cite approved sources; general "
                             "answers are labelled as not from the curriculum.",
    "nexa.safety.teacher/1": "Teachers and school staff: professional content; may read teacher-only "
                             "material; no learner personal data in prompts; curriculum answers must cite "
                             "approved sources; declines harmful requests.",
    "nexa.safety.service/1": "Service-to-service agents with no human user in the loop: no free-form chat "
                             "with learners; outputs are reviewed by the calling service.",
}
POLICY_FOR_AUDIENCE = {"student": "nexa.safety.student/1", "teacher": "nexa.safety.teacher/1"}


# ---------------------------------------------------------------- platform rules

def _known_models() -> set[str]:
    """Chat models the Model Gateway knows: its live registry when this process
    has one, otherwise the defaults plus the repository's example registry."""
    from v1.model_gateway import registry as gw
    names = {e.logical for e in gw.REGISTRY.values() if e.capability == "chat"}
    names |= {e.logical for e in gw.DEFAULT_MODELS if e.capability == "chat"}
    path = os.path.join(os.path.dirname(__file__), "..", "model_gateway", "models.example.json")
    try:
        with open(path) as fh:
            names |= {e.logical for e in gw.parse_models(json.load(fh)) if e.capability == "chat"}
    except (OSError, ValueError, gw.RegistryError):
        pass
    return names


def _known_tools() -> dict[str, Any]:
    from . import tools as tool_registry
    return dict(tool_registry.TOOLS)


def validate_platform(m: AgentManifest, *, models: Optional[set[str]] = None,
                      tools: Optional[dict[str, Any]] = None,
                      others: Optional[dict[str, AgentManifest]] = None) -> list[str]:
    """Rules that need the rest of NEXA. Returns a list of problems (empty: valid)."""
    from v1.identity.rbac import ROLE_PERMISSIONS
    problems: list[str] = []
    models = _known_models() if models is None else models
    tools = _known_tools() if tools is None else tools
    all_perms = set().union(*ROLE_PERMISSIONS.values())
    for name in [m.model.model, *m.model.fallbacks]:
        if name not in models:
            problems.append(f"model {name!r} is not a logical model in the Model Gateway")
    if m.permissions.run_permission not in all_perms:
        problems.append(f"run_permission {m.permissions.run_permission!r} is not a NEXA permission")
    for role in m.permissions.allowed_roles:
        if role not in ROLE_PERMISSIONS:
            problems.append(f"role {role!r} is not a NEXA role")
        elif m.permissions.run_permission not in ROLE_PERMISSIONS[role]:
            problems.append(f"role {role!r} does not hold {m.permissions.run_permission!r}, so it could never run "
                            f"the agent")
    for t in m.tools:
        if t.name not in tools:
            problems.append(f"tool {t.name!r} is not registered in the tool registry")
        elif m.runnable and not getattr(tools[t.name], "available", True):
            problems.append(f"tool {t.name!r} is not available yet, so a {m.lifecycle.state.value} agent may not "
                            f"be granted it")
        elif t.permission != getattr(tools[t.name], "required_permission", t.permission):
            problems.append(f"tool {t.name!r}: the manifest says {t.permission!r} but the tool requires "
                            f"{tools[t.name].required_permission!r}")
        if t.permission not in all_perms:
            problems.append(f"tool {t.name!r}: permission {t.permission!r} is not a NEXA permission")
    if m.safety.policy not in SAFETY_POLICIES:
        problems.append(f"safety policy {m.safety.policy!r} is not a NEXA safety policy")
    serves_students = Audience.student in m.purpose.audience or "student" in m.permissions.allowed_roles
    if serves_students and m.safety.policy != POLICY_FOR_AUDIENCE["student"]:
        problems.append("an agent for students must run under nexa.safety.student/1")
    if serves_students:
        if not m.safety.serves_minors:
            problems.append("an agent for students must declare serves_minors: true (senior secondary learners "
                            "include minors)")
        if "teacher_only" in m.permissions.knowledge.access_classes or \
                "restricted" in m.permissions.knowledge.access_classes:
            problems.append("an agent for students may only read learner content")
        if m.memory.long_term == "read_write":
            problems.append("an agent for students may not write long-term memory under nexa.agent/1.0")
    if m.permissions.knowledge.access_classes and "knowledge_service" not in m.health.requires:
        problems.append("an agent that reads knowledge must require knowledge_service in health")
    if m.lifecycle.replaced_by:
        if others is not None and m.lifecycle.replaced_by not in others:
            problems.append(f"replaced_by {m.lifecycle.replaced_by!r} is not a known agent")
        if m.lifecycle.replaced_by == m.id:
            problems.append("an agent cannot replace itself")
    if m.runtime.timeout_s < m.runtime.retry.backoff_s * (m.runtime.retry.max_attempts - 1):
        problems.append("timeout_s is shorter than the retry backoff alone")
    return problems


# ---------------------------------------------------------------- loading

class ManifestError(Exception):
    def __init__(self, path: str, problems: list[str]):
        self.path, self.problems = path, problems
        super().__init__(f"{path}: " + "; ".join(problems))


def parse(path: str) -> AgentManifest:
    with open(path) as fh:
        raw = json.load(fh)
    try:
        return AgentManifest.model_validate(raw)
    except ValidationError as exc:
        raise ManifestError(path, [f"{'.'.join(str(x) for x in e['loc']) or '(manifest)'}: {e['msg']}"
                                   for e in exc.errors()]) from None


def load_dir(directory: str = AGENTS_DIR, *, check_platform: bool = True) -> dict[str, AgentManifest]:
    """Every manifest in the directory, all checks applied. One bad manifest
    stops the load: the runtime never starts with an agent nobody validated."""
    found: dict[str, AgentManifest] = {}
    for f in sorted(os.listdir(directory)):
        if not f.endswith(".json"):
            continue
        path = os.path.join(directory, f)
        m = parse(path)
        if f != f"{m.id}.json":
            raise ManifestError(path, [f"file must be named {m.id}.json"])
        if m.id in found:
            raise ManifestError(path, [f"duplicate agent id {m.id}"])
        found[m.id] = m
    if check_platform:
        for m in found.values():
            problems = validate_platform(m, others=found)
            if problems:
                raise ManifestError(os.path.join(directory, f"{m.id}.json"), problems)
    return found


def json_schema() -> dict[str, Any]:
    s = AgentManifest.model_json_schema()
    s["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    s["$id"] = "https://nexa.learnx.ai/schemas/nexa-agent-manifest-1.0.schema.json"
    s["title"] = "NEXA agent manifest (nexa.agent/1.0)"
    return s


def main(argv: list[str]) -> int:
    if len(argv) >= 1 and argv[0] == "schema":
        print(json.dumps(json_schema(), indent=2))
        return 0
    if len(argv) >= 1 and argv[0] == "validate":
        paths = argv[1:] or [os.path.join(AGENTS_DIR, f) for f in sorted(os.listdir(AGENTS_DIR)) if f.endswith(".json")]
        parsed: dict[str, AgentManifest] = {}
        bad = 0
        for p in paths:
            try:
                parsed[p] = parse(p)
            except ManifestError as exc:
                bad += 1
                print(f"INVALID {p}")
                for x in exc.problems:
                    print(f"  - {x}")
        others = {m.id: m for m in parsed.values()}
        for p, m in parsed.items():
            problems = validate_platform(m, others=others)
            if problems:
                bad += 1
                print(f"INVALID {p}")
                for x in problems:
                    print(f"  - {x}")
            else:
                print(f"valid   {p}  {m.id} {m.identity.version} [{m.lifecycle.state.value}] {m.fingerprint()[:19]}")
        return 1 if bad else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
